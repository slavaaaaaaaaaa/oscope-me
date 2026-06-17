"""FileSource decoding options."""

from unittest.mock import MagicMock, patch

import pytest

from oscope_me.file_source import FileSource


@pytest.fixture
def wav_path(tmp_path):
    path = tmp_path / "test.wav"
    path.write_bytes(b"RIFF")  # existence check only; ffmpeg is mocked
    return path


def _mock_ffmpeg_process():
    mock_proc = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.readline.return_value = b""
    return mock_proc


def test_start_offset_in_ffmpeg_command(wav_path):
    src = FileSource(wav_path, 48_000, start_offset_seconds=30)
    with patch("oscope_me.file_source.subprocess.Popen",
               return_value=_mock_ffmpeg_process()) as popen:
        src.start()
    cmd = popen.call_args[0][0]
    i_idx = cmd.index("-i")
    assert cmd[i_idx - 2] == "-ss"
    assert float(cmd[i_idx - 1]) == 30.0
    assert src.last_cmd == cmd


def test_no_seek_flag_at_zero_offset(wav_path):
    src = FileSource(wav_path, 48_000)
    with patch("oscope_me.file_source.subprocess.Popen",
               return_value=_mock_ffmpeg_process()) as popen:
        src.start()
    cmd = popen.call_args[0][0]
    assert "-ss" not in cmd


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
