"""The Backend release candidate (D08 B2.5): a reviewed hash lock for the complete Python
dependency closure, the producer that certifies one exact source and one exact set of
package files inside the required CI check, and the non-privileged consumer that
reconstructs them offline in a separate disposable environment. See release/README.md.

Stdlib only. The consumer must be able to run before anything has been installed.
"""
