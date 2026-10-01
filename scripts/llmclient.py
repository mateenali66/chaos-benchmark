#!/usr/bin/env python3
"""
Amazon Bedrock client for Component 3 (fault selection, run-campaign.py's
LLMStrategy) and Component 4 (hypothesis generation,
run-hypothesis-generation.py).

Uses only the bedrock-runtime converse API, which gives one request and
response shape for the three providers in experiments/llm-config.yaml
(Anthropic, Meta, Mistral), so there are no provider-specific request bodies.

boto3 and botocore are imported lazily, so this module and run-campaign.py
import without boto3 installed. The tests use a mock client with the same
invoke() signature and never construct BedrockClient.

The region comes from experiments/llm-config.yaml (ca-central-1, where the
three model ids were checked). The AWS profile comes from the AWS_PROFILE
environment variable.
"""

import os
import time
from dataclasses import dataclass
from typing import Optional

DEFAULT_REGION = "ca-central-1"

# Bedrock error codes retried with backoff. Any other error (auth,
# validation, access denied) fails on the first attempt.
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
    """Normalized converse() result.

    raw is the full boto3 response, kept for the transcript, which is the
    reproducibility record because Bedrock has no sampling seed common to all
    three providers.
    """

    text: str
    model_id: str
    raw: dict
    stop_reason: Optional[str] = None


class BedrockClient:
    """Wrapper over bedrock-runtime converse, bound to one model id.

    Each arm in experiments/llm-config.yaml gets its own instance. Errors in
    RETRYABLE_ERROR_CODES are retried with exponential backoff
    (base_delay_s * 2**(attempt-1)), for at most `retries` attempts. Any other
    ClientError, or a retryable one on the last attempt, raises
    BedrockInvokeError.
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
        temperature_stripped = False
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
                msg = str(e)
                # Some models, including global.anthropic.claude-sonnet-5,
                # reject an explicit temperature with "`temperature` is
                # deprecated for this model". Retry once without it, which
                # uses one of the attempts.
                if (not temperature_stripped and code == "ValidationException"
                        and "temperature" in msg and "deprecated" in msg):
                    kwargs["inferenceConfig"].pop("temperature", None)
                    temperature_stripped = True
                    continue
                if code not in RETRYABLE_ERROR_CODES or attempt == self.retries:
                    raise BedrockInvokeError(
                        f"bedrock-runtime converse failed for {self.model_id} "
                        f"(attempt {attempt}/{self.retries}, code={code!r}): {e}"
                    ) from e
                time.sleep(self.base_delay_s * (2 ** (attempt - 1)))
        # Reached only if the temperature retry above used the last attempt.
        raise BedrockInvokeError(
            f"bedrock-runtime converse failed for {self.model_id} after "
            f"{self.retries} attempts: {last_error}"
        )
