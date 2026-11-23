# A Toy Paper on Residual Connections

## 1 Introduction

We add a residual connection [3] around each sub-layer, followed by layer normalization. The model does not use recurrence.

As shown in Fig. 2, the training loss drops below 0.15 after at least 20 epochs, which is 3.5× faster than the baseline in Table 1.

## 2 Method

Each layer computes $y = x + f(x)$, where the residual connection keeps the gradient path short (see Section 2.1).
