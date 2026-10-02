"""A value typed to the bot fills its place in a JSON request and nothing else (review 0.25.0, B1.5).

Plain substitution let `1,"limit":999999` or `x","target":"OTHER_SERVER` close the number or string
it was put in and override keys the template fixed (`json.loads` keeps the last duplicate).
"""

from __future__ import annotations

import json

from db_ops.telegram.command_processor import render_template

TEMPLATE = '{"sql_id": {sql_id}, "limit": {limit}, "target": "{server_id}", "confirm": false}'


def test_ordinary_values_render_as_before():
    rendered = json.loads(render_template(TEMPLATE, {"sql_id": "12", "limit": "50", "server_id": "SRV-1"}))
    assert rendered == {"sql_id": 12, "limit": 50, "target": "SRV-1", "confirm": False}


def test_a_number_slot_cannot_add_a_key():
    rendered = json.loads(render_template(TEMPLATE, {"sql_id": '1,"limit":999999', "limit": "50",
                                                     "server_id": "SRV-1"}))
    assert rendered["limit"] == 50 and rendered["sql_id"] == '1,"limit":999999'


def test_a_string_slot_cannot_close_the_string():
    rendered = json.loads(render_template(TEMPLATE, {"sql_id": "1", "limit": "5",
                                                     "server_id": 'x","confirm":true,"target":"OTHER'}))
    assert rendered["confirm"] is False
    assert rendered["target"] == 'x","confirm":true,"target":"OTHER'


def test_a_part_that_is_not_json_is_filled_as_text():
    assert render_template("--server={server_id}", {"server_id": "SRV-1"}) == "--server=SRV-1"
