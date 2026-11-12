"""True CTC forward-backward and a zero-output temporal residual MLP.

This module performs no I/O, inference, training or data loading on import.
It is a research implementation; production model files are never modified.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import numpy as np


def log_softmax(logits):
    """Stable last-axis log probabilities, retaining the caller's precision."""
    maximum = np.max(logits, axis=-1, keepdims=True)
    shifted = logits - maximum
    return shifted - np.log(np.exp(shifted).sum(axis=-1, keepdims=True))


def sequence_edit_distance(left, right):
    """Levenshtein distance for character error rate without model dependencies."""
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index]
        for other, target in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[other] + 1, previous[other - 1] + (char != target)))
        previous = current
    return previous[-1]


def ctc_loss_and_grad(logits, labels, *, blank=0, sample_weights=None):
    """Marginal CTC loss and exact gradient over all valid alignment paths.

    ``logits`` is N,T,C. Targets contain no blank. Per-image negative log
    likelihood is divided by target length, then sample weights are averaged.
    Alpha includes its current-frame emission; beta excludes that emission.
    """
    array = np.asarray(logits)
    if array.ndim != 3 or not np.isfinite(array).all():
        raise ValueError('CTC requires finite N,T,C logits')
    batch, frames, classes = array.shape
    if len(labels) != batch or not 0 <= blank < classes:
        raise ValueError('CTC target batch or blank index is invalid')
    if not batch or not frames:
        raise ValueError('CTC requires a nonempty batch and at least one frame')
    lengths = np.asarray([len(label) for label in labels], dtype=np.int64)
    for label in labels:
        if any(not 0 <= value < classes or value == blank for value in label):
            raise ValueError('CTC targets must be valid nonblank class indices')
        required = len(label) + sum(left == right for left, right in pairwise(label))
        if frames < required:
            raise ValueError('Not enough CTC frames for the target and repeated characters')
    states = 2 * int(lengths.max(initial=0)) + 1
    target = np.full((batch, states), blank, dtype=np.int64)
    valid = np.arange(states)[None] <= (2 * lengths[:, None])
    for index, label in enumerate(labels):
        target[index, 1:2 * len(label):2] = label
    skip = np.zeros((batch, states), dtype=bool)
    skip[:, 2:] = (target[:, 2:] != blank) & (target[:, 2:] != target[:, :-2]) & valid[:, 2:]
    probabilities = log_softmax(array.astype(np.float64))
    emissions = np.take_along_axis(probabilities, target[:, None, :], axis=-1)
    alpha = np.full((batch, frames, states), -np.inf, dtype=np.float64)
    alpha[:, 0, 0] = emissions[:, 0, 0]
    if states > 1:
        alpha[:, 0, 1] = np.where(lengths > 0, emissions[:, 0, 1], -np.inf)
    for frame in range(1, frames):
        previous = alpha[:, frame - 1]
        advance = np.full_like(previous, -np.inf)
        advance[:, 1:] = previous[:, :-1]
        jump = np.full_like(previous, -np.inf)
        jump[:, 2:] = np.where(skip[:, 2:], previous[:, :-2], -np.inf)
        summed = np.logaddexp(np.logaddexp(previous, advance), jump)
        alpha[:, frame] = np.where(valid, summed + emissions[:, frame], -np.inf)
    indices = np.arange(batch)
    final_blank = alpha[indices, -1, 2 * lengths]
    final_char = alpha[indices, -1, np.maximum(0, 2 * lengths - 1)]
    log_z = np.logaddexp(final_blank, np.where(lengths > 0, final_char, -np.inf))
    if not np.isfinite(log_z).all():
        raise ValueError('CTC target has no finite alignment')
    beta = np.full_like(alpha, -np.inf)
    beta[indices, -1, 2 * lengths] = 0
    nonempty = lengths > 0
    beta[indices[nonempty], -1, 2 * lengths[nonempty] - 1] = 0
    for frame in reversed(range(frames - 1)):
        onward = beta[:, frame + 1] + emissions[:, frame + 1]
        advance = np.full_like(onward, -np.inf)
        advance[:, :-1] = onward[:, 1:]
        jump = np.full_like(onward, -np.inf)
        jump[:, :-2] = np.where(skip[:, 2:], onward[:, 2:], -np.inf)
        beta[:, frame] = np.where(valid, np.logaddexp(np.logaddexp(onward, advance), jump), -np.inf)
    occupancy = np.exp(alpha + beta - log_z[:, None, None])
    one_hot = np.eye(classes, dtype=np.float64)[target] * valid[:, :, None]
    posterior = occupancy @ one_hot
    weights = np.ones(batch, dtype=np.float64) if sample_weights is None else np.asarray(sample_weights, dtype=np.float64)
    if weights.shape != (batch,) or not np.isfinite(weights).all() or np.any(weights < 0) or weights.sum() <= 0:
        raise ValueError('CTC sample weights must be finite, nonnegative and nonzero')
    factors = weights / (weights.sum() * np.maximum(1, lengths))
    gradient = (np.exp(probabilities) - posterior) * factors[:, None, None]
    loss = float(-np.sum(log_z * factors))
    return loss, gradient.astype(array.dtype), {'per_image_nll': -log_z, 'posterior': posterior}


def context_window(features, radius=1):
    """Concatenate local temporal neighbors with edge replication."""
    if features.ndim != 3 or radius < 0:
        raise ValueError('Temporal features must be N,T,D and radius nonnegative')
    frames = features.shape[1]
    padded = np.pad(features, ((0, 0), (radius, radius), (0, 0)), mode='edge')
    return np.concatenate([padded[:, offset:offset + frames] for offset in range(2 * radius + 1)], axis=-1)


@dataclass
class TemporalResidual:
    """A bounded residual adapter; a zero final projection preserves baseline."""

    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: np.ndarray
    radius: int = 1
    cap: float = 3.0

    @classmethod
    def initialize(cls, dimensions=120, hidden=48, classes=63, *, radius=1, cap=3.0, seed=20261112):
        """Initialize hidden weights reproducibly and output weights to zero."""
        if min(dimensions, hidden, classes) <= 0 or radius < 0 or cap <= 0:
            raise ValueError('Invalid residual adapter dimensions')
        random = np.random.default_rng(seed)
        inputs = dimensions * (2 * radius + 1)
        w1 = random.normal(0, np.sqrt(2 / (inputs + hidden)), (inputs, hidden)).astype(np.float32)
        return cls(w1, np.zeros(hidden, np.float32), np.zeros((hidden, classes), np.float32),
                   np.zeros(classes, np.float32), radius, cap)

    def parameters(self):
        """Named mutable tensors for an optimizer or an external checkpoint."""
        return {'w1': self.w1, 'b1': self.b1, 'w2': self.w2, 'b2': self.b2}

    def forward(self, features, baseline_logits):
        """Return corrected logits and the exact intermediate values for gradients."""
        context = context_window(features, self.radius)
        hidden = np.tanh(context @ self.w1 + self.b1)
        raw = hidden @ self.w2 + self.b2
        bounded = np.tanh(raw / self.cap)
        residual = self.cap * bounded
        return baseline_logits + residual, {'context': context, 'hidden': hidden,
                                            'bounded': bounded, 'residual': residual}

    def backward(self, grad_logits, cache, *, residual_penalty=0.0):
        """Backpropagate through the adapter only; the backbone is frozen."""
        residual = cache['residual']
        gradient = grad_logits + (2 * residual_penalty / residual.size) * residual
        raw_gradient = gradient * (1 - cache['bounded'] ** 2)
        hidden_gradient = (raw_gradient @ self.w2.T) * (1 - cache['hidden'] ** 2)
        context = cache['context'].reshape(-1, self.w1.shape[0])
        hidden = cache['hidden'].reshape(-1, self.w1.shape[1])
        raw_gradient = raw_gradient.reshape(-1, self.w2.shape[1])
        hidden_gradient = hidden_gradient.reshape(-1, self.w1.shape[1])
        return {'w1': context.T @ hidden_gradient, 'b1': hidden_gradient.sum(axis=0),
                'w2': hidden.T @ raw_gradient, 'b2': raw_gradient.sum(axis=0)}


def teacher_kl_and_grad(logits, baseline_logits, protected):
    """Framewise teacher consistency for baseline-correct ordinary train images."""
    protected = np.asarray(protected, dtype=np.float64)
    if protected.shape != (len(logits),) or not np.isfinite(protected).all() or np.any(protected < 0):
        raise ValueError('Protection mask must be one nonnegative weight per image')
    if protected.sum() == 0:
        return 0.0, np.zeros_like(logits)
    teacher = log_softmax(baseline_logits.astype(np.float64))
    student = log_softmax(logits.astype(np.float64))
    normalization = protected[:, None, None] / (protected.sum() * logits.shape[1])
    probability = np.exp(teacher)
    loss = float(np.sum(normalization * probability * (teacher - student)))
    gradient = normalization * (np.exp(student) - probability)
    return loss, gradient.astype(logits.dtype)


class Adam:
    """Small deterministic Adam optimizer with global gradient clipping."""

    def __init__(self, parameters, *, learning_rate=1e-3, beta1=.9, beta2=.999, epsilon=1e-8, clip_norm=1.0):
        self.parameters = parameters
        self.learning_rate = learning_rate
        self.beta1, self.beta2 = beta1, beta2
        self.epsilon, self.clip_norm = epsilon, clip_norm
        self.first = {name: np.zeros_like(value) for name, value in parameters.items()}
        self.second = {name: np.zeros_like(value) for name, value in parameters.items()}
        self.steps = 0

    def step(self, gradients):
        """Apply one update, rejecting nonfinite gradients before changing weights."""
        if set(gradients) != set(self.parameters) or any(not np.isfinite(value).all() for value in gradients.values()):
            raise ValueError('Invalid optimizer gradients')
        norm = np.sqrt(sum(float(np.sum(value.astype(np.float64) ** 2)) for value in gradients.values()))
        scale = min(1.0, self.clip_norm / max(norm, 1e-12))
        self.steps += 1
        for name, parameter in self.parameters.items():
            gradient = gradients[name] * scale
            self.first[name] = self.beta1 * self.first[name] + (1 - self.beta1) * gradient
            self.second[name] = self.beta2 * self.second[name] + (1 - self.beta2) * gradient * gradient
            first = self.first[name] / (1 - self.beta1 ** self.steps)
            second = self.second[name] / (1 - self.beta2 ** self.steps)
            parameter[...] -= self.learning_rate * first / (np.sqrt(second) + self.epsilon)
        return {'gradient_norm': norm, 'gradient_clip_scale': scale}
