import json

from jp import kernel_proto as kp


def test_build_header_has_required_fields():
    h = kp.build_header("execute_request", session="sess-1", msg_id="m-1")
    assert h["msg_type"] == "execute_request"
    assert h["session"] == "sess-1"
    assert h["msg_id"] == "m-1"
    assert h["version"] == "5.3"
    assert "date" in h and "username" in h


def test_execute_request_parts_are_four_json_dicts():
    parts = kp.build_execute_request("print(1)", session="s", msg_id="m")
    assert len(parts) == 4  # header, parent_header, metadata, content
    header = json.loads(parts[0])
    content = json.loads(parts[3])
    assert header["msg_type"] == "execute_request"
    assert content["code"] == "print(1)"
    assert content["silent"] is False
    assert content["allow_stdin"] is False
    assert content["stop_on_error"] is True


def test_comm_msg_carries_data_and_marks_buffers():
    parts, nbuf = kp.build_comm_msg(
        comm_id="c1",
        data={"op": "read"},
        buffers=[b"\x00\xff"],
        session="s",
        msg_id="m",
    )
    assert nbuf == 1
    content = json.loads(parts[3])
    assert content["comm_id"] == "c1"
    assert content["data"] == {"op": "read"}
    assert len(parts) == 4
