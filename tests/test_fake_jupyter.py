from jp import fsrpc
from jp import kernel_proto as kp
from jp._sim import FakeKernelWS


def test_fake_ws_roundtrips_a_comm_msg_through_the_real_agent(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"abcdef")
    ws = FakeKernelWS(root=str(tmp_path))  # boots the real Agent on tmp_path
    ws.open_comm(comm_id="c1", target="jp.fs")

    parts, nbuf = kp.build_comm_msg(
        "c1",
        fsrpc.request(fsrpc.OP_READ, rid=1, path="f.txt", offset=1, length=3),
        session="s",
        msg_id="m1",
    )
    ws.send_binary(kp.pack_ws_v1("shell", parts))  # client sends a comm_msg

    msgs = ws.read_messages()  # server (agent) replies
    assert len(msgs) == 1
    channel, blobs = kp.unpack_ws_v1(msgs[0])
    import json

    content = json.loads(blobs[3])
    assert content["data"]["ok"] is True
    assert blobs[4] == b"bcd"  # bytes in the binary buffer
