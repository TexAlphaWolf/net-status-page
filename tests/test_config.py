from pathlib import Path

import pytest

from upstatus.config import EXAMPLE, ConfigError, load_config, parse_config


def conf(extra: str, site: str = "") -> str:
    return f"{site}\n[[check]]\nname = 'a'\n{extra}\n"


def parse(text: str):
    return parse_config(text, Path("/base"))


def test_the_example_is_valid():
    cfg = parse(EXAMPLE)
    assert [c.type for c in cfg.checks] == ["http", "tls", "dns", "ping", "tcp"]
    assert cfg.site.output == Path("/base/public")
    assert cfg.site.retention_days == 90
    assert cfg.notify.on == ("down", "up")
    http = cfg.checks[0]
    assert (http.host, http.tls, http.path, http.expect_status, http.contains) == (
        "example.org",
        True,
        "/",
        (200,),
        "Example Domain",
    )
    assert cfg.checks[0].interval == 60 and cfg.checks[0].fail_after == 3


def test_defaults_are_overridden_per_check():
    cfg = parse(
        "[defaults]\ninterval='5m'\nfail_after=4\n[[check]]\nname='a'\ntype='ping'\nhost='1.1.1.1'\n"
        "[[check]]\nname='b'\ntype='ping'\nhost='1.1.1.2'\nfail_after=2\ninterval=30\n"
    )
    assert (cfg.checks[0].interval, cfg.checks[0].fail_after) == (300, 4)
    assert (cfg.checks[1].interval, cfg.checks[1].fail_after) == (30, 2)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not toml [",
        "[site]\ntitle='x'\n",  # no check
        "[unknown]\nx=1\n[[check]]\nname='a'\ntype='ping'\nhost='h'\n",
        "[site]\nbogus=1\n" + conf("type='ping'\nhost='h'"),
        conf("type='ftp'\nhost='h'"),
        conf("type='ping'"),  # no host
        conf("type='ping'\nhost='-x'"),
        conf("type='ping'\nhost='h'\ntypo=1"),
        conf("type='tcp'\nhost='h'"),  # no port
        conf("type='tcp'\nhost='h'\nport=0"),
        conf("type='tcp'\nhost='h'\nport=70000"),
        conf("type='tcp'\nhost='h'\nport=true"),
        conf("type='http'\nurl='ftp://x/'"),
        conf("type='http'\nurl='https://user:pw@x/'"),
        conf("type='http'\nurl='https://x y/'"),
        conf("type='http'\nurl='https:///nohost'"),
        conf("type='http'\nurl='https://x/'\nexpect_status=[99]"),
        conf("type='http'\nurl='https://x/'\nexpect_status='200'"),
        conf("type='ping'\nhost='h'\nurl='https://x/'"),
        conf("type='ping'\nhost='h'\ncontains='x'"),
        conf("type='ping'\nhost='h'\ninterval='2s'"),
        conf("type='ping'\nhost='h'\ninterval='3d'"),
        conf("type='ping'\nhost='h'\ninterval='10s'\ntimeout=10"),
        conf("type='ping'\nhost='h'\ntimeout=0"),
        conf("type='ping'\nhost='h'\nfail_after=0"),
        conf("type='ping'\nhost='h'\nrecover_after=99"),
        conf("type='ping'\nhost='h'\nslow_factor=1"),
        conf("type='ping'\nhost='h'\ndegrade='yes'"),
        conf("type='tls'\nhost='h'\nmin_days=-1"),
        conf("type='dns'\nhost='h'\nexpect='not an address'"),
        '[[check]]\nname="a\\u0000b"\ntype="ping"\nhost="h"\n',
        "[[check]]\nname=''\ntype='ping'\nhost='h'\n",
        "[[check]]\nname='" + "x" * 61 + "'\ntype='ping'\nhost='h'\n",
        "[[check]]\nname='a'\ntype='ping'\nhost='h'\n[[check]]\nname='A'\ntype='ping'\nhost='h'\n",  # same name
        "[notify]\non=['down','explode']\n" + conf("type='ping'\nhost='h'"),
        "[notify]\non=[]\n" + conf("type='ping'\nhost='h'"),
        "[notify]\nwebhook_env='https://hooks.example/x'\n" + conf("type='ping'\nhost='h'"),
        "[notify]\nwebhook='https://hooks.example/x'\n" + conf("type='ping'\nhost='h'"),  # a secret: never in the file
        "[site]\nretention_days=1\n" + conf("type='ping'\nhost='h'"),
        "[site]\ntitle=5\n" + conf("type='ping'\nhost='h'"),
    ],
)
def test_mistakes_are_refused(text):
    with pytest.raises(ConfigError):
        parse(text)


def test_error_messages_name_the_check():
    with pytest.raises(ConfigError, match="'a'"):
        parse(conf("type='tcp'\nhost='h'"))


def test_url_parts():
    c = parse(conf("type='http'\nurl='http://Example.org:8080/a/b?x=1'")).checks[0]
    assert (c.host, c.port, c.tls, c.path) == ("example.org", 8080, False, "/a/b?x=1")
    c = parse(conf("type='http'\nurl='https://[2001:db8::1]/'")).checks[0]
    assert c.host == "2001:db8::1"


def test_tls_defaults():
    c = parse(conf("type='tls'\nhost='h'")).checks[0]
    assert (c.port, c.min_days) == (443, 14)


def test_load_reads_a_file_and_resolves_paths_next_to_it(tmp_path):
    f = tmp_path / "u.toml"
    f.write_text(EXAMPLE, encoding="utf-8")
    cfg = load_config(f)
    assert cfg.site.data == tmp_path.resolve() / "upstatus-data"


def test_load_errors(tmp_path):
    with pytest.raises(ConfigError, match="upstatus init"):
        load_config(tmp_path / "missing.toml")
    (tmp_path / "bin.toml").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(tmp_path / "bin.toml")
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path)  # a directory


def test_too_many_checks():
    rows = "".join(f"[[check]]\nname='c{i}'\ntype='ping'\nhost='h'\n" for i in range(501))
    with pytest.raises(ConfigError, match="500"):
        parse(rows)
