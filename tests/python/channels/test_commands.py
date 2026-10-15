from backend.app.channels.commands import control_command, pairing_code


def test_pairing_code_accepts_case_insensitive_command_and_bare_code():
    assert pairing_code(" /PAIR meido-abcdef12 ") == "MEIDO-ABCDEF12"
    assert pairing_code("/pair=MEIDO-ABCDEF12") == "MEIDO-ABCDEF12"
    assert pairing_code("meido-abcdef12") == "MEIDO-ABCDEF12"
    assert pairing_code("hello MEIDO-ABCDEF12") is None


def test_control_command_accepts_telegram_bot_suffix_and_exact_commands():
    assert control_command(" /STOP@meido_bot ") == "stop"
    assert control_command("/chatid") == "chatid"
    assert control_command("/myid") == "myid"
    assert control_command("/stop everything") is None
