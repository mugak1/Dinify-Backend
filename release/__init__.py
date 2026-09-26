"""The Backend release candidate (D08 B2.5): a reviewed hash lock for the complete Python
dependency closure, the producer that certifies one exact source and one exact set of
package files inside the required CI check, and the non-privileged consumer that
reconstructs them offline in a separate disposable environment; and (D08 B2.6) the
non-deploying preflight that re-establishes one retained candidate's certification from the
GitHub API, asks the advisory question again over its retained inventory under the trusted
policy, and a receiving check that reproduces that decision. See release/README.md.

Stdlib only. The consumer must be able to run before anything has been installed.
"""
