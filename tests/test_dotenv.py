"""The local-development `.env` loader (config.load_dotenv): convenient, but must never override or leak anything."""
import pytest

from config import ConfigError, Settings, load_dotenv


def write(tmp_path, text, name=".env"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_plain_values_comments_quotes_and_export(tmp_path):
    p = write(tmp_path, "\n".join([
        "# a comment", "", "AUTH_MODE=google", "GOOGLE_CLIENT_ID = abc.apps.googleusercontent.com  ",
        'SECRET_KEY="quoted value with spaces"', "ALLOWED_DOMAIN='example.com'", "export AWS_MODE=fake",
        "PUBLIC_BASE_URL=http://localhost:5000   # trailing comment", "HASH_INSIDE=abc#def", "EMPTY=",
    ]))
    env = {}
    names = load_dotenv(p, env)
    assert env == {"AUTH_MODE": "google", "GOOGLE_CLIENT_ID": "abc.apps.googleusercontent.com",
                   "SECRET_KEY": "quoted value with spaces", "ALLOWED_DOMAIN": "example.com", "AWS_MODE": "fake",
                   "PUBLIC_BASE_URL": "http://localhost:5000", "HASH_INSIDE": "abc#def", "EMPTY": ""}
    assert names == list(env)                                   # returns names only, in file order


def test_real_environment_variables_always_win(tmp_path):
    p = write(tmp_path, "AUTH_MODE=google\nAWS_MODE=fake\n")
    env = {"AUTH_MODE": "dev"}
    assert load_dotenv(p, env) == ["AWS_MODE"]
    assert env == {"AUTH_MODE": "dev", "AWS_MODE": "fake"}


@pytest.mark.parametrize("flag", [{"RAILWAY_ENVIRONMENT": "production"}, {"PRODUCTION": "1"}, {"L180_SKIP_DOTENV": "1"}])
def test_never_runs_in_production_or_when_skipped(tmp_path, flag):
    p = write(tmp_path, "AUTH_MODE=dev\nSECRET_KEY=from-file\n")
    env = dict(flag)
    assert load_dotenv(p, env) == []
    assert env == flag                                          # nothing was added


def test_a_production_flag_inside_the_file_cannot_switch_the_loader_off_or_on(tmp_path):
    p = write(tmp_path, "RAILWAY_ENVIRONMENT=production\nAUTH_MODE=dev\n")
    env = {}
    load_dotenv(p, env)                                         # only the REAL environment decides whether to load
    assert env["AUTH_MODE"] == "dev"


def test_ignores_garbage_lines_and_does_not_expand_variables(tmp_path):
    p = write(tmp_path, "no equals sign\n=novalue\n1BAD=x\nBAD KEY=x\nA=$HOME\nB=%PATH%\nC=${A}\n")
    env = {}
    load_dotenv(p, env)
    assert env == {"A": "$HOME", "B": "%PATH%", "C": "${A}"}    # literal text, never expanded


def test_missing_file_is_fine(tmp_path):
    assert load_dotenv(tmp_path / "nope.env", {}) == []


def test_utf8_bom_is_accepted_and_utf16_gets_a_helpful_error(tmp_path):
    bom = tmp_path / "bom.env"
    bom.write_bytes(b"\xef\xbb\xbfAUTH_MODE=dev\n")             # what Notepad's "UTF-8 with BOM" writes
    env = {}
    load_dotenv(bom, env)
    assert env == {"AUTH_MODE": "dev"}
    utf16 = tmp_path / "utf16.env"
    utf16.write_bytes("AUTH_MODE=dev\n".encode("utf-16"))        # what PowerShell's `>` redirection writes
    with pytest.raises(ConfigError, match="UTF-8"):
        load_dotenv(utf16, {})


def test_error_messages_never_contain_secret_values(tmp_path):
    p = write(tmp_path, "GOOGLE_CLIENT_ID=\nGOOGLE_CLIENT_SECRET=super-secret-value\nAUTH_MODE=google\n")
    env = {"CLIENTS_FILE": str(tmp_path.parent.parent / "clients.example.json")}
    load_dotenv(p, env)
    with pytest.raises(ConfigError) as e:
        Settings.from_env(env)
    assert "GOOGLE_CLIENT_ID" in str(e.value) and "super-secret-value" not in str(e.value)


def test_explicit_environment_mappings_never_read_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr("config.BASE_DIR", tmp_path)
    write(tmp_path, "AUTH_MODE=google\n")
    s = Settings.from_env({"CLIENTS_JSON": '{"clients":[{"slug":"a","name":"A","region":"ap-south-1","ec2_instances":[{"id":"i-0123456789abcdef0"}]}]}'})
    assert s.auth_mode == "dev"                                 # tests pass a dict: the developer's .env is not consulted
