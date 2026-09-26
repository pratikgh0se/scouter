"""Hyperbolic Time Chamber: a product-agnostic release gauntlet engine.

taxonomy -> generator -> runner -> evaluator -> report -> fix loop.
Stdlib only. Products plug in through `gauntlet.adapter.Adapter`; this
package never imports a product (rows 436, 437)."""

__version__ = "1.0.0"
