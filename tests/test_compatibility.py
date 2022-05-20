# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import pytest

from pex.compatibility import PY3, indent, to_bytes, to_unicode

unicode_string = (str,) if PY3 else (unicode,)  # type: ignore[name-defined]


def test_to_bytes():
    # type: () -> None
    assert isinstance(to_bytes(""), bytes)
    assert isinstance(to_bytes("abc"), bytes)
    assert isinstance(to_bytes(b"abc"), bytes)
    assert isinstance(to_bytes(u"abc"), bytes)
    assert isinstance(to_bytes(b"abc".decode("latin-1"), encoding=u"utf-8"), bytes)

    for bad_value in (123, None):
        with pytest.raises(ValueError):
            to_bytes(bad_value)  # type: ignore[type-var]


def test_to_unicode():
    # type: () -> None
    assert isinstance(to_unicode(""), unicode_string)
    assert isinstance(to_unicode("abc"), unicode_string)
    assert isinstance(to_unicode(b"abc"), unicode_string)
    assert isinstance(to_unicode(u"abc"), unicode_string)
    assert isinstance(to_unicode(u"abc".encode("latin-1"), encoding=u"latin-1"), unicode_string)

    for bad_value in (123, None):
        with pytest.raises(ValueError):
            to_unicode(bad_value)  # type: ignore[type-var]


def test_indent():
    # type: () -> None
    assert "  line1" == indent("line1", "  ")

    assert "  line1\n  line2" == indent("line1\nline2", "  ")
    assert "  line1\n  line2\n" == indent("line1\nline2\n", "  ")

    assert "  line1\n\n  line3" == indent("line1\n\nline3", "  ")
    assert "  line1\n \n  line3" == indent("line1\n \nline3", "  ")
    assert "  line1\n  \n  line3" == indent("line1\n\nline3", "  ", lambda line: True)
