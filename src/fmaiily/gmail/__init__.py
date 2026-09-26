"""Gmail-facing seams: MIME construction and the transport protocol.

Nothing outside this package may import ``googleapiclient``; the boundary test
``tests/unit/test_import_boundaries.py`` enforces it.
"""
