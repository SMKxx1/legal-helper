"""soffice docx→PDF conversion (PLAN §3.10) — driven against a FAKE ``soffice`` script, zero real deps.

The real LibreOffice binary isn't available in CI, so these tests point ``soffice_bin`` at a tiny shell
script that mimics its ``--convert-to pdf --outdir <dir> <input>`` contract (writes a minimal PDF), and
exercise the success + every typed-failure path (missing binary, non-zero exit, timeout). The tmpdir
isolation + the fixed on-disk input name are asserted implicitly (the fake reads ``--outdir`` / the input
path the module passes).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.config import Settings
from app.integrations.convert import (
    ConversionError,
    ConversionTimeout,
    ConversionUnavailable,
    convert_to_pdf,
)

MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _write_script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return str(path)


@pytest.fixture
def fake_soffice(tmp_path):
    """A ``soffice`` that writes a minimal PDF named ``<input-stem>.pdf`` into ``--outdir``."""
    script = tmp_path / "soffice_ok.sh"
    # Walk args: capture the value AFTER --outdir, and treat the final arg as the input path.
    body = (
        'outdir=""\n'
        'prev=""\n'
        'input=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--outdir" ]; then outdir="$a"; fi\n'
        '  input="$a"\n'
        '  prev="$a"\n'
        "done\n"
        'stem=$(basename "$input")\n'
        'stem="${stem%.*}"\n'
        'printf "%%PDF-1.4\\n1 0 obj<<>>endobj\\ntrailer<<>>\\n%%%%EOF\\n" > "$outdir/$stem.pdf"\n'
        "exit 0\n"
    )
    return _write_script(script, body)


@pytest.fixture
def _settings(tmp_path):
    return Settings(_env_file=None, data_dir=str(tmp_path / "data"))


def test_convert_docx_to_pdf_ok(fake_soffice, _settings):
    out = convert_to_pdf(
        b"PK\x03\x04 fake docx bytes",
        filename="Signed NDA.docx",
        settings=_settings,
        soffice_bin=fake_soffice,
    )
    assert out.startswith(b"%PDF")


def test_convert_missing_binary_is_unavailable(_settings):
    with pytest.raises(ConversionUnavailable):
        convert_to_pdf(
            b"data",
            filename="x.docx",
            settings=_settings,
            soffice_bin="/nonexistent/soffice-binary",
        )


def test_convert_nonzero_exit_is_error(tmp_path, _settings):
    bad = _write_script(tmp_path / "soffice_fail.sh", "exit 3\n")
    with pytest.raises(ConversionError):
        convert_to_pdf(b"data", filename="x.docx", settings=_settings, soffice_bin=bad)


def test_convert_no_output_is_error(tmp_path, _settings):
    # Exits 0 but writes no PDF — the "produced no PDF output" guard.
    noop = _write_script(tmp_path / "soffice_noop.sh", "exit 0\n")
    with pytest.raises(ConversionError):
        convert_to_pdf(b"data", filename="x.docx", settings=_settings, soffice_bin=noop)


def test_convert_timeout(tmp_path, _settings):
    slow = _write_script(tmp_path / "soffice_slow.sh", "sleep 5\n")
    with pytest.raises(ConversionTimeout):
        convert_to_pdf(
            b"data",
            filename="x.docx",
            settings=_settings,
            soffice_bin=slow,
            timeout_s=0.4,
        )


def test_convert_empty_input_rejected(_settings, fake_soffice):
    with pytest.raises(ConversionError):
        convert_to_pdf(
            b"", filename="x.docx", settings=_settings, soffice_bin=fake_soffice
        )


def test_convert_uses_settings_defaults(fake_soffice, tmp_path):
    # soffice_bin resolved from settings when not passed explicitly.
    s = Settings(_env_file=None, data_dir=str(tmp_path / "d"), soffice_bin=fake_soffice)
    out = convert_to_pdf(b"data", filename="a.rtf", settings=s)
    assert out.startswith(b"%PDF")


def test_convert_input_suffix_reaches_disk_not_client_name(
    fake_soffice, _settings, tmp_path
):
    # The client filename never becomes the on-disk input name (only its suffix does). We assert the
    # conversion still succeeds for a hostile filename — the module builds a fixed ``input<suffix>``.
    out = convert_to_pdf(
        b"data",
        filename="../../etc/passwd.docx",
        settings=_settings,
        soffice_bin=fake_soffice,
    )
    assert out.startswith(b"%PDF")
    # No stray file was written under the cwd from the traversal-shaped name.
    assert not (Path(os.getcwd()) / ".." / ".." / "etc" / "passwd.pdf").exists()
