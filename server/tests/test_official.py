from app import official


def test_reads_days_with_rows_in_various_time_formats():
    payload = [
        {"WeekDay": 1, "Rows": [{"StartTime": 25200000, "EndTime": 30600000, "ComfortTemperature": 21.5}]},     # lundi, ms
        {"WeekDay": 0, "Rows": [{"StartTime": "17:15:00", "EndTime": "22:30:00", "ComfortTemperature": 21}]},    # dimanche, texte
        {"WeekDay": "Tuesday", "Rows": [{"StartTime": 420, "DurationMilliSeconds": 5400000}]},                  # minutes + durée
    ]
    assert official.normalize(payload) == [
        {"day": 0, "start": "07:00", "end": "08:30", "setpoint": 21.5},
        {"day": 1, "start": "07:00", "end": "08:30", "setpoint": None},
        {"day": 6, "start": "17:15", "end": "22:30", "setpoint": 21.0},
    ]


def test_tolerates_unknown_shapes():
    assert official.normalize(None) == [] and official.normalize({"Items": "x"}) == []
    assert official.normalize([{"WeekDay": 9, "Rows": [{"StartTime": 1, "EndTime": 2}]}]) == []
    assert official.shape([{"WeekDay": 1, "Rows": [{"StartTime": 1, "EndTime": 2, "SensorSetId": "secret"}]}])["sample"][0]["Rows"][0].get("SensorSetId") is None
