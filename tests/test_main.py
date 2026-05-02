from app.main import main


def test_main_outputs_message(capsys):
    main()
    captured = capsys.readouterr()
    assert "Hello from the new Python project!" in captured.out
