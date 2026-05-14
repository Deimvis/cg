import logging
import subprocess as sp
from pathlib import Path


IMPL_ROOT_SQL_FOLDER = "root-sql-folder"
IMPL_EMBEDDED_SQL_FOLDER = "embedded-sql-folder"
IMPLS = [IMPL_EMBEDDED_SQL_FOLDER, IMPL_ROOT_SQL_FOLDER]
DEFAULT_IMPL = IMPL_EMBEDDED_SQL_FOLDER


CODEGEN_FILE_HEADER = "// THIS CODE IS GENERATED - DO NOT CHANGE IT"

CODEGEN_DEPS_EMBEDDED = (
    """

var sqlFolder embed.FS = storages.SqlFolder

"""
    + """

func validateSyntax(query string) error {
	if strings.Contains(query, "$$") {
		return nil
	}
	validateQuery := fmt.Sprintf(`
		DO $SYNTAX_CHECK$ BEGIN RETURN;
		%s;
		END; $SYNTAX_CHECK$;`,
		query)
	_, err := setup.PG.Exec(context.Background(), validateQuery)
	return err
}

func validate(queryRelPath string, msgAndArgs ...interface{}) func() error {
	return func() error {
		query := string(xmust.Do(sqlFolder.ReadFile("sql/" + queryRelPath)))
		for _, query := range xmust.Do(xsql.ParseQueries(query)) {
			err := validateSyntax(query)
			if err != nil {
				return fmt.Errorf("%s has invalid sql: %w", queryRelPath, err)
			}
		}
		return nil
	}
}
"""
)

CODEGEN_DEPS_ROOT = """

func validateSyntax(query string) error {
	if strings.Contains(query, "$$") {
		return nil
	}
	validateQuery := fmt.Sprintf(`
		DO $SYNTAX_CHECK$ BEGIN RETURN;
		%s;
		END; $SYNTAX_CHECK$;`,
		query)
	_, err := setup.PG.Exec(context.Background(), validateQuery)
	return err
}

func validate(sqlQuery string, msgAndArgs ...interface{}) func() error {
	return func() error {
		for i, query := range xmust.Do(xsql.ParseQueries(sqlQuery)) {
			err := validateSyntax(query)
			if err != nil {
				msg := xfmt.Sprintfg(msgAndArgs...)
				return fmt.Errorf("%s (query #%d): %w", msg, i+1, err)
			}
		}
		return nil
	}
}
"""


def _split_words(s: str) -> list[str]:
    words: list[str] = []
    buf = ""
    for c in s:
        if c in ["_", "-"]:
            words.append(buf)
            buf = ""
        elif c.isupper():
            words.append(buf)
            buf = c
        else:
            buf += c
    if len(buf) > 0:
        words.append(buf)
    return words


def _camel_case(s: str) -> str:
    result = "".join(w.capitalize() for w in _split_words(s))
    if "Abc" in result:
        result = result.replace("Abc", "ABC")
    if "Vk" in result:
        result = result.replace("Vk", "VK")
    if "Acl" in result:
        result = result.replace("Acl", "ACL")
    if "Md5" in result:
        result = result.replace("Md5", "MD5")
    if "Ttl" in result:
        result = result.replace("Ttl", "TTL")
    if result == "Eq":
        result = "EQ"
    if result == "Ge":
        result = "GE"
    if result == "Le":
        result = "LE"
    return result


def _process_dir_embedded(sql_dp: Path, p: Path) -> list[str]:
    result: list[str] = []
    for child in p.iterdir():
        if child.is_dir():
            result.extend(_process_dir_embedded(sql_dp, child))
        elif child.is_file():
            rel_fp = child.relative_to(sql_dp)
            result.append(f'validate("{rel_fp}"),')
        else:
            raise RuntimeError(f"Got unknown entity type: {child.absolute()}")
    return result


def _process_dir_root(sql_dp: Path, p: Path) -> list[str]:
    result: list[str] = []
    for child in p.iterdir():
        if child.is_dir():
            result.extend(_process_dir_root(sql_dp, child))
        elif child.is_file():
            rel_fp = child.relative_to(sql_dp)
            tr_ext = lambda part: part[: part.rfind(".")] if "." in part else part
            field_name = ".".join(_camel_case(tr_ext(part)) for part in rel_fp.parts)
            result.append(f'validate(m.{field_name}, "{rel_fp}"),')
        else:
            raise RuntimeError(f"Got unknown entity type: {child.absolute()}")
    return result


def _run_embedded(sql_dir: Path, output_file: Path) -> None:
    body = "\n".join(
        sorted(_process_dir_embedded(sql_dir, sql_dir), key=lambda x: (-x.count("."), x))
    )
    code = f"""
    {CODEGEN_FILE_HEADER}
    package sql_tests

    {CODEGEN_DEPS_EMBEDDED}

    func queryValidates() []func() error {{
        return []func() error {{
            {body}
        }}
    }}
    """
    _write(code, output_file)


def _run_root(sql_dir: Path, output_file: Path) -> None:
    body = "\n".join(
        sorted(_process_dir_root(sql_dir, sql_dir), key=lambda x: (-x.count("."), x))
    )
    code = f"""
    {CODEGEN_FILE_HEADER}
    package sql_tests

    {CODEGEN_DEPS_ROOT}

    func queryValidates(m *sql.QueryManager) []func() error {{
        return []func() error {{
            {body}
        }}
    }}
    """
    _write(code, output_file)


def _write(code: str, output_file: Path) -> None:
    logging.info(code)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w") as f:
        f.write(code)
    sp.run(["gofmt", "-s", "-w", output_file.absolute()])
    sp.run(["goimports", "-w", output_file.absolute()])


def run(sql_dir: Path, output_file: Path, impl: str = DEFAULT_IMPL) -> None:
    assert sql_dir.is_dir()
    if impl == IMPL_EMBEDDED_SQL_FOLDER:
        _run_embedded(sql_dir, output_file)
    elif impl == IMPL_ROOT_SQL_FOLDER:
        _run_root(sql_dir, output_file)
    else:
        raise ValueError(f"unsupported sql_tests_go impl: {impl}")
