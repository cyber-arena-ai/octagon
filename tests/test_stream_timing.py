"""Stream-timing sidecar: `_stamp_chunk` pins arrival wall-clock per stream
line (line_no aligned with the main stream) so metrics can derive per-tool-call
latency and token rate. Pure/offline — no docker."""

from __future__ import annotations

import io

from agents.transport_docker import _stamp_chunk


def test_multiple_lines_in_one_read_share_the_read_instant():
    buf = bytearray()
    sink = io.StringIO()
    ln = _stamp_chunk(buf, b'{"a":1}\n{"b":2}\n', 100.0, 0, sink)
    assert ln == 2
    assert sink.getvalue() == "1\t100.0000\n2\t100.0000\n"
    assert bytes(buf) == b""  # nothing partial carried over


def test_line_split_across_reads_is_stamped_once_at_completion():
    buf = bytearray()
    sink = io.StringIO()
    # first read: a partial line, no newline yet -> nothing stamped, carried over
    ln = _stamp_chunk(buf, b'{"tool_use', 10.0, 0, sink)
    assert ln == 0 and sink.getvalue() == ""
    assert bytes(buf) == b'{"tool_use'
    # second read completes line 1 and starts a partial line 2
    ln = _stamp_chunk(buf, b'":"x"}\n{"next"', 12.5, ln, sink)
    assert ln == 1
    assert sink.getvalue() == "1\t12.5000\n"  # stamped at the read that completed it
    assert bytes(buf) == b'{"next"'


def test_line_numbers_align_with_stream_across_arbitrary_boundaries():
    buf = bytearray()
    sink = io.StringIO()
    ln = 0
    stream = b"".join(f'{{"i":{i}}}\n'.encode() for i in range(5))
    for i in range(0, len(stream), 7):  # feed in odd 7-byte chunks
        ln = _stamp_chunk(buf, stream[i:i + 7], float(i), ln, sink)
    assert ln == 5
    nums = [int(line.split("\t")[0]) for line in sink.getvalue().splitlines()]
    tss = [float(line.split("\t")[1]) for line in sink.getvalue().splitlines()]
    assert nums == [1, 2, 3, 4, 5]
    assert tss == sorted(tss)  # monotonic non-decreasing
