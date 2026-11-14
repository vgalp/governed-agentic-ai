"""Continuous training for the guard classifier and the answering model (ADR 037).

A frontier model writes and labels data; local models train on it; every round is measured on held-out
gate sets and recorded in a ledger; a person promotes a model only after its sealed checks. See
`python -m modelops --help`.
"""
