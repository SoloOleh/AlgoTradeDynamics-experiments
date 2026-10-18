"""The exchange seam: the one interface strategies and runtimes reach an exchange through.

Only `exchange/<name>/` sub-packages may import an exchange client (`pybit`, `EmulatorHTTP`) or
open an exchange WS connection (sad.md §5).
"""
