import logging
import subprocess as sp
from pathlib import Path


CODEGEN_FILE_HEADER = "// THIS CODE IS GENERATED - DO NOT CHANGE IT"
CODEGEN_DEPS = (
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


def _process_dir(sql_dp: Path, p: Path) -> list[str]:
    result: list[str] = []
    for child in p.iterdir():
        if child.is_dir():
            result.extend(_process_dir(sql_dp, child))
        elif child.is_file():
            rel_fp = child.relative_to(sql_dp)
            result.append(f'validate("{rel_fp}"),')
        else:
            raise RuntimeError(f"Got unknown entity type: {child.absolute()}")
    return result


def run(sql_dir: Path, output_file: Path) -> None:
    assert sql_dir.is_dir()
    body = "\n".join(sorted(_process_dir(sql_dir, sql_dir), key=lambda x: (-x.count("."), x)))
    code = f"""
    {CODEGEN_FILE_HEADER}
    package sql_tests

    {CODEGEN_DEPS}

    func queryValidates() []func() error {{
        return []func() error {{
            {body}
        }}
    }}
    """
    logging.info(code)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w") as f:
        f.write(code)
    sp.run(["gofmt", "-s", "-w", output_file.absolute()])
    sp.run(["goimports", "-w", output_file.absolute()])
