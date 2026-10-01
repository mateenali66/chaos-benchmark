#!/usr/bin/env python3
"""
Component 5 anomaly detectors (analysis/PREREGISTRATION.md, Component 5).

Each detector function has the signature
    detector(X: np.ndarray (T, F), baseline_mask: np.ndarray (T,) bool) -> np.ndarray (T,)
It is fit only on X[baseline_mask], the run's own baseline phase, and then
scores all T timesteps of that run. Every run gets a freshly fit detector
and nothing is learned across runs, so no fault data reaches training.
Higher score = more anomalous.

Detectors (frozen list, PREREGISTRATION.md Component 5): EWMA control
chart, Isolation Forest, reconstruction autoencoder, Deep SVDD.
static_threshold_baseline() is the comparator.
"""
from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

TORCH_SEED = 42


def _fit_scaler(X: np.ndarray, baseline_mask: np.ndarray) -> StandardScaler:
    scaler = StandardScaler()
    baseline = X[baseline_mask]
    # Guard against a zero-variance feature (e.g. pod_restarts_total flat at
    # 0 for the whole baseline) making StandardScaler divide by ~0.
    scaler.fit(baseline)
    scaler.scale_[scaler.scale_ < 1e-8] = 1.0
    return scaler


def ewma_control_chart(X: np.ndarray, baseline_mask: np.ndarray, alpha: float = 0.3) -> np.ndarray:
    """EWMA control chart. Per-feature mean and variance start at the
    baseline mean and variance, then update online through the whole series,
    as a deployed chart would, so a sustained shift is tracked. Each
    timestep's score is the maximum absolute per-feature z-score against the
    running EWMA mean and standard deviation."""
    scaler = _fit_scaler(X, baseline_mask)
    Xs = scaler.transform(X)
    baseline = Xs[baseline_mask]

    mu = baseline.mean(axis=0)
    var = baseline.var(axis=0)
    var = np.maximum(var, 1e-6)

    scores = np.zeros(len(Xs))
    for t in range(len(Xs)):
        z = np.abs(Xs[t] - mu) / np.sqrt(var)
        scores[t] = z.max()
        mu = alpha * Xs[t] + (1 - alpha) * mu
        var = alpha * (Xs[t] - mu) ** 2 + (1 - alpha) * var
        var = np.maximum(var, 1e-6)
    return scores


def isolation_forest_detector(X: np.ndarray, baseline_mask: np.ndarray) -> np.ndarray:
    scaler = _fit_scaler(X, baseline_mask)
    Xs = scaler.transform(X)
    n_baseline = int(baseline_mask.sum())
    model = IsolationForest(
        n_estimators=100,
        max_samples=min(256, n_baseline),
        random_state=TORCH_SEED,
    )
    model.fit(Xs[baseline_mask])
    # decision_function: higher = more normal. Flip sign so higher = more anomalous.
    return -model.decision_function(Xs)


class _TinyAutoencoder(nn.Module):
    def __init__(self, n_features: int):
        super().__init__()
        hidden = max(2, n_features // 2)
        bottleneck = max(1, n_features // 4)
        self.encoder = nn.Sequential(
            nn.Linear(n_features, hidden), nn.ReLU(),
            nn.Linear(hidden, bottleneck), nn.ReLU(),
        )
        self.decoder = nn.Sequential(
            nn.Linear(bottleneck, hidden), nn.ReLU(),
            nn.Linear(hidden, n_features),
        )

    def forward(self, x):
        return self.decoder(self.encoder(x))


def reconstruction_autoencoder(X: np.ndarray, baseline_mask: np.ndarray,
                                epochs: int = 200, lr: float = 1e-2) -> np.ndarray:
    """Small feedforward autoencoder (bottleneck ~ n_features/4) fit on the
    baseline segment, about 60 points per run. Score = per-timestep
    reconstruction MSE over the whole run. Trained full-batch with Adam for
    a fixed number of epochs. There is no validation split: the per-run
    baseline is too small to hold points out, and no other run's data may
    be used."""
    torch.manual_seed(TORCH_SEED)
    scaler = _fit_scaler(X, baseline_mask)
    Xs = scaler.transform(X).astype(np.float32)

    model = _TinyAutoencoder(Xs.shape[1])
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.MSELoss()

    baseline_t = torch.from_numpy(Xs[baseline_mask])
    model.train()
    for _ in range(epochs):
        opt.zero_grad()
        recon = model(baseline_t)
        loss = loss_fn(recon, baseline_t)
        loss.backward()
        opt.step()

    model.eval()
    with torch.no_grad():
        all_t = torch.from_numpy(Xs)
        recon = model(all_t)
        scores = ((recon - all_t) ** 2).mean(dim=1).numpy()
    return scores


class _SVDDEncoder(nn.Module):
    def __init__(self, n_features: int):
        super().__init__()
        hidden = max(2, n_features // 2)
        out = max(2, n_features // 2)
        self.net = nn.Sequential(
            nn.Linear(n_features, hidden), nn.ReLU(),
            nn.Linear(hidden, out),
        )

    def forward(self, x):
        return self.net(x)


def deep_svdd(X: np.ndarray, baseline_mask: np.ndarray,
              epochs: int = 200, lr: float = 1e-2) -> np.ndarray:
    """One-class Deep SVDD (Ruff et al. 2018), without the soft boundary. An
    encoder maps baseline points as close as possible to a fixed center c,
    the mean of an untrained forward pass over the baseline. As in the
    original paper, c is fixed before training to avoid a collapsed
    solution. Score = squared distance from c over the whole run."""
    torch.manual_seed(TORCH_SEED)
    scaler = _fit_scaler(X, baseline_mask)
    Xs = scaler.transform(X).astype(np.float32)

    model = _SVDDEncoder(Xs.shape[1])
    baseline_t = torch.from_numpy(Xs[baseline_mask])

    model.eval()
    with torch.no_grad():
        c = model(baseline_t).mean(dim=0)
    # Avoid centers exactly at 0 in any dimension (degenerate collapse guard
    # from the original paper).
    c[(c.abs() < 1e-4)] = 1e-4

    opt = torch.optim.Adam(model.parameters(), lr=lr)
    model.train()
    for _ in range(epochs):
        opt.zero_grad()
        out = model(baseline_t)
        loss = ((out - c) ** 2).sum(dim=1).mean()
        loss.backward()
        opt.step()

    model.eval()
    with torch.no_grad():
        all_t = torch.from_numpy(Xs)
        scores = ((model(all_t) - c) ** 2).sum(dim=1).numpy()
    return scores


def static_threshold_baseline(X: np.ndarray, baseline_mask: np.ndarray,
                               feature_names: list[str]) -> np.ndarray:
    """Static-threshold comparator, adapted from Component 3's weakness
    signals (PREREGISTRATION.md, Component 5 amendment item 2).

    Component 3's rule needs per-timestep HTTP error rate and p99, which this
    data does not have (see component5_features.py). The adaptation keeps
    the two legs that have a per-timestep infrastructure analogue:
      - pod restarts: pod_restarts_total above the run's own baseline mean.
      - resource spike: any other feature (CPU, memory, network) above 3x
        its own baseline mean, the "p99 > 3x baseline" structure applied to
        resource usage.
    Score = number of legs violated at that timestep (0, 1 or 2).

    This is a new rule, not Component 3's formula. Its score is constant on
    420 of 719 runs, and component5_evaluate.py excludes those runs from the
    confirmatory test."""
    baseline_mean = X[baseline_mask].mean(axis=0)
    baseline_mean_safe = np.where(np.abs(baseline_mean) < 1e-9, 1e-9, baseline_mean)

    restart_idx = feature_names.index("pod_restarts_total") if "pod_restarts_total" in feature_names else None
    resource_idx = [i for i, n in enumerate(feature_names) if n != "pod_restarts_total"]

    scores = np.zeros(len(X))
    for t in range(len(X)):
        v = 0
        if restart_idx is not None and X[t, restart_idx] > baseline_mean[restart_idx] + 1e-9:
            v += 1
        ratio = np.abs(X[t, resource_idx] / baseline_mean_safe[resource_idx])
        if (ratio > 3.0).any():
            v += 1
        scores[t] = v
    return scores


DETECTORS = {
    "ewma": lambda X, mask, names: ewma_control_chart(X, mask),
    "isolation_forest": lambda X, mask, names: isolation_forest_detector(X, mask),
    "autoencoder": lambda X, mask, names: reconstruction_autoencoder(X, mask),
    "deep_svdd": lambda X, mask, names: deep_svdd(X, mask),
}
BASELINE_NAME = "static_threshold"
