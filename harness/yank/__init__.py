"""Pull the disk out mid-write, and prove nothing that was saved was lost.

The workload writes the way an agent's drive is written: databases committing,
configuration replaced atomically, memory appended, files copied. Every write is
logged on *another* disk the moment it is acknowledged, so after a pull the
question "was anything saved lost?" is answered from a record rather than a
guess. The verifier then checks the recovered drive against that record.
"""
