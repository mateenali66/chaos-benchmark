#!/usr/bin/env python3
"""
Bedrock client for Components 3 (fault selection, run-campaign.py's
LLMStrategy) and 4 (hypothesis generation, run-hypothesis-generation.py) of
the ML fault-selection study. Wraps boto3's bedrock-runtime `converse` API
only -- converse normalizes message/output shape across the three frozen
providers (Anthropic, Meta, Mistral; experiments/llm-config.yaml) so this
module carries no provider-specific request bodies.

boto3 is imported lazily inside BedrockClient.__init__ (and botocore inside
invoke()) so this module -- and anything that imports it, e.g.
run-campaign.py -- stays importable in environments where boto3 is not
installed. LLMStrategy and the hypothesis-generation script are exercised in
tests exclusively through a mock client implementing the same `.invoke()`
signature; BedrockClient itself is never constructed in tests.

Region and profile: region is passed explicitly (ca-central-1 per
experiments/llm-config.yaml, the only region the three frozen model IDs were
verified invokable in); profile comes from the AWS_PROFILE environment
variable per this repo's convention, never hardcoded.
"""

import os
import time
from dataclasses import dataclass
from typing import Optional

DEFAULT_REGION = "ca-central-1"

# Bedrock error codes worth retrying with backoff; anything else (auth,
# validation, access-denied, model-not-ready) fails fast on attempt 1.
RETRYABLE_ERROR_CODES = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "ModelNotReadyException",
    "InternalServerException",
}


class BedrockInvokeError(RuntimeError):
    """Raised after all retries are exhausted, or on a non-retryable
    ClientError, calling converse()."""


@dataclass
class LLMResponse:
    """Normalized converse() result. `raw` is the full boto3 response dict,
    kept for transcript logging (reproducibility depends on it -- Bedrock
    does not expose a sampling seed across all three providers, so the
    request/response transcript is the reproducibility record)."""

    text: str
    model_id: str
    raw: dict
    stop_reason: Optional[str] = None


class BedrockClient:
    """Thin wrapper over bedrock-runtime `converse`, bound to one model id
    (an LLMStrategy or hypothesis-generation arm binds to exactly one; the
    three arms in experiments/llm-config.yaml each get their own instance).
    Retries up to `retries` times with exponential backoff
    (base_delay_s * 2**(attempt-1)) on the throttling/transient error codes
    in RETRYABLE_ERROR_CODES; any other ClientError, or a retryable one with
    no attempts left, raises BedrockInvokeError.
    """

    def __init__(self, model_id: str, region: str = DEFAULT_REGION,
                 profile: Optional[str] = None, retries: int = 3,
                 base_delay_s: float = 1.0):
        import boto3  # lazy: keep this module importable without boto3 installed

        self.model_id = model_id
        self.region = region
        self.retries = retries
        self.base_delay_s = base_delay_s
        resolved_profile = profile if profile is not None else os.environ.get("AWS_PROFILE")
        session = boto3.Session(profile_name=resolved_profile, region_name=region)
        self._runtime = session.client("bedrock-runtime", region_name=region)

    def invoke(self, prompt: str, temperature: float = 0.2, max_tokens: int = 1024,
               system: Optional[str] = None) -> LLMResponse:
        from botocore.exceptions import ClientError  # lazy alongside boto3

        messages = [{"role": "user", "content": [{"text": prompt}]}]
        kwargs = {
            "modelId": self.model_id,
            "messages": messages,
            "inferenceConfig": {"temperature": temperature, "maxTokens": max_tokens},
        }
        if system:
            kwargs["system"] = [{"text": system}]

        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self._runtime.converse(**kwargs)
                text = "".join(
                    block["text"]
                    for block in response["output"]["message"]["content"]
                    if "text" in block
                )
                return LLMResponse(
                    text=text,
                    model_id=self.model_id,
                    raw=response,
                    stop_reason=response.get("stopReason"),
                )
            except ClientError as e:
                last_error = e
                code = e.response.get("Error", {}).get("Code", "")
                if code not in RETRYABLE_ERROR_CODES or attempt == self.retries:
                    raise BedrockInvokeError(
                        f"bedrock-runtime converse failed for {self.model_id} "
                        f"(attempt {attempt}/{self.retries}, code={code!r}): {e}"
                    ) from e
                time.sleep(self.base_delay_s * (2 ** (attempt - 1)))
        # Unreachable in practice (the loop always returns or raises), kept
        # as a defensive fallback so this function cannot silently return None.
        raise BedrockInvokeError(
            f"bedrock-runtime converse failed for {self.model_id} after "
            f"{self.retries} attempts: {last_error}"
        )
