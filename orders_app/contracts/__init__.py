"""Source-authoritative exports of contracts other repositories compile against.

A contract in here is a fact this repository DECIDES and another one must agree
with. Each module names exactly one, derives it from the production constants that
enforce it — never by typing the numbers a second time — and offers a canonical
form whose digest a peer can reproduce without sharing any code with us.
"""
