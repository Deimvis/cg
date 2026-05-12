import subprocess as sp
import os
import urllib.parse
import yaml
from enum import Enum
from pathlib import Path

from . import remote

# TODO: support redefining settings for whole file, or for models matching regex in this file
# TODO: emergent
# 1) add Content-Type header for generated structs
# 2) validate that body stream io.Reader is created only once

# TODO: support structured headers: allow only plain types or aliases to plain types
#       golang: make Headers fields a custom struct (type parameter), make Header() method to return shallow copy of current headers.
#               add field Headers.EXTRA which is http.Header and allows to add extra headers (+ validate that openapi spec doesnt have it)
#
# TODO: support error when required includes field that does not exist in properties
# TODO: support generation of files in separate location and swapping current files to new only when validation check succeeds
#       For golang: allow copying only module root, because import path consists module name, and on swapping module name should be changed
#       Do not swap files if their content is same to help lsp do not lose index
# TODO: support nestsed settings like: x-cg-go: { def: ... field-name: ... } it will be equivalent to x-cg-go-def: + x-cg-go-field-name: ...
# TODO: support custom x-cg-go-obj-name (for renaming current object)
# TODO: support adding private fields to generated struct
# TODO: support enum embedding with allOf (one enum embeds other enums). it should work with proper comparison of variables - ordinary golang embedding doesn't work this way.
# TODO: somehow link request to response in codegen to allow validation that response refers to request
# TODO: define abbrevs for in-file scope (e.g. SecretsRemovalResponseCode -> SRemovalRC)
# TODO: define camel-case for in-file scope (e.g. ACCESS_TOKEN -> AccessToken)
# TODO: add golang imports explicitly to avoid cases when golang can't find package for import
# TODO: support pushing all API definitions into single file (handle_api_file -> into one file)
# TODO: support comments generation from field descriptions or explicitly with language-specific rules (like write above or on the same line)
# TODO: support yaml anchors (ensure swagger will work properly)
# TODO: support x-cg-ignore: skip definition
# TODO: support description into comments convertion
# TODO: support x-cg-short-uri-name: for uri parameters (like for /secrets/:id you want property to have name SecretId, but to be able to specify short name, in order to use it as uri tag: SecretId string `uri:"id"`)
# TODO: support inline: doesn't generate separate struct, just inline everywhere it is used
# TODO: support camel case for abbreviations
# TODO: support x-cg-go-field-name
# TODO: support x-cg-go-def
# TODO: do not create files that do not have effective schemas (e.g. when all schemas were replaced with x-cg-go-def)


MODELS = Path.cwd() / 'src/models'
MODELS_TMP = Path.cwd() / 'tmp/models'

VOLUMES: list[tuple[Path | str, Path]] = [
    # custom; each entry is (src, dst) where `src` is a local Path or a URL string
    # prefix (http(s)://...), and `dst` is the local output directory Path.
   ((Path.cwd() / 'docs/openapi/definitions'), MODELS),
]

DEFAULT_VOLUME: dict[Path, Path] = {
    (Path.cwd() / 'docs/openapi'): MODELS / 'api',
}


CODEGEN_FILE_HEADER = '// THIS CODE IS GENERATED - DO NOT CHANGE IT'
CODEGEN_FILE_NAME_PREFIX = ''
# CODEGEN_FILE_NAME_SUFFIX = '.gen'
# TODO: migrate in a separate commit
CODEGEN_FILE_NAME_SUFFIX = '.cg'

FW_IMPORT_PATH = os.environ.get('FW_IMPORT_PATH', 'github.com/Deimvis-go/fw')

# Prefixes for custom openapi extension properties (e.g. `x-cg-go-def`,
# `x-cg-header`). The first prefix is the canonical one used in error messages;
# the remaining prefixes are aliases accepted on input. Override with the
# comma-separated `CG_OPENAPI_PROPERTY_PREFIX` env var, e.g. `x-cg,x-vk`.
PROP_PREFIXES: list[str] = [
    p.strip() for p in os.environ.get('CG_OPENAPI_PROPERTY_PREFIX', 'x-cg').split(',') if p.strip()
] or ['x-cg']


_MISSING = object()


def prop_get(schema: dict, suffix: str, default=None):
    """Return the value of the first `<prefix>-<suffix>` key present in `schema`,
    trying each configured prefix in order. Returns `default` if none match."""
    for prefix in PROP_PREFIXES:
        key = f'{prefix}-{suffix}'
        if key in schema:
            return schema[key]
    return default


def prop_in(schema: dict, suffix: str) -> bool:
    return prop_get(schema, suffix, _MISSING) is not _MISSING


def prop_canonical(suffix: str) -> str:
    """Canonical form of the property name, using the first configured prefix.
    For error messages and docs."""
    return f'{PROP_PREFIXES[0]}-{suffix}'

PROCESSED_FILES = []
GENERATED_OUT_FILES = []


def _find_git_root_fp(fp: Path) -> Path:
    cur = fp.absolute()
    while True:
        git = cur / '.git'
        # git not necessarily dir;
        # in case of worktree it is a regular file
        # containing path to original git dir.
        if git.exists():
            return cur

        if cur.parent == cur:
            break
        cur = cur.parent
    raise RuntimeError(f'failed to find git root file path from {fp}')


def with_extension(fp: Path, ext: str) -> Path:
    return fp.parent / (CODEGEN_FILE_NAME_PREFIX + fp.stem + CODEGEN_FILE_NAME_SUFFIX + ext)

def _url_parents(url: str) -> list[str]:
    """Return URL prefixes from deepest to shallowest, e.g.
    https://x/a/b/c.yaml -> [https://x/a/b, https://x/a, https://x]."""
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split('/') if p]
    if not parts:
        return []
    out = []
    for i in range(len(parts) - 1, 0, -1):
        prefix_path = '/' + '/'.join(parts[:i])
        out.append(urllib.parse.urlunparse(parsed._replace(path=prefix_path)))
    out.append(urllib.parse.urlunparse(parsed._replace(path='')))
    return out


def _url_relative(url: str, prefix: str) -> str:
    """Return the path of `url` relative to URL prefix `prefix`. Both must
    share scheme+netloc and `url` must be under `prefix`."""
    p_url = urllib.parse.urlparse(url)
    p_prefix = urllib.parse.urlparse(prefix)
    if (p_url.scheme, p_url.netloc) != (p_prefix.scheme, p_prefix.netloc):
        raise ValueError(f'{url!r} is not under {prefix!r}')
    url_path = p_url.path.rstrip('/')
    prefix_path = p_prefix.path.rstrip('/')
    if not url_path.startswith(prefix_path + '/'):
        raise ValueError(f'{url!r} is not under {prefix!r}')
    return url_path[len(prefix_path) + 1:]


def _match_volume(openapi_fp: Path) -> tuple[Path, Path] | None:
    """Pick the closest matching volume for `openapi_fp` and return
    (relative_source, output_dir) where `relative_source` is the file
    path relative to the volume's source root. Returns None if nothing matches."""
    source_url = remote.url_of_cache_path(openapi_fp)

    matches: list[tuple[int, Path, str]] = []  # (distance, output_dir, relative_source_str)

    if source_url is not None:
        url_parents = _url_parents(source_url)
        for src, output_dir in VOLUMES:
            if not isinstance(src, str):
                continue
            for i, parent in enumerate(url_parents):
                if parent.rstrip('/') == src.rstrip('/'):
                    rel = _url_relative(source_url, src)
                    matches.append((i, output_dir, rel))
                    break
    else:
        for src, output_dir in VOLUMES:
            if not isinstance(src, Path):
                continue
            for i in range(len(openapi_fp.parents)):
                if openapi_fp.parents[i] == src:
                    rel = str(openapi_fp.relative_to(src))
                    matches.append((i, output_dir, rel))
                    break
        if not matches:
            for src, output_dir in DEFAULT_VOLUME.items():
                for i in range(len(openapi_fp.parents)):
                    if openapi_fp.parents[i] == src:
                        rel = str(openapi_fp.relative_to(src))
                        matches.append((i, output_dir, rel))
                        break

    if not matches:
        return None
    matches.sort(key=lambda m: m[0])
    _, output_dir, rel = matches[0]
    return Path(rel), output_dir


def get_out_fp(openapi_fp: Path) -> Path:
    # TODO: recursively regenerate each dependency (openapi fp)
    #       Use explicit destinations specifiedd inside openapi fp when exists (must specify main that will be included by others or default resolve policy: import closest from file who requested this definition)
    #       if they aren't present -> try to resolve using volumes mappings
    #       if no output_fp found -> error.
    #       Common rule:
    #       - libraries (any common dependencies for multiple openapi_fps) use explicit destinations,
    #       - end-specs (like reset api) use volumes

    # check explicit destinations

    assert openapi_fp.exists(), f'openapi file doesnt exist: {openapi_fp}'
    assert openapi_fp.is_file()
    assert openapi_fp.suffix == '.yaml'
    with openapi_fp.open('r') as f:
        content = yaml.safe_load(f)
    header = prop_get(content, 'header')
    if header is not None:
        if 'explicit_codegen_destinations' in header:
            ecds = header['explicit_codegen_destinations']
            # TODO: support other lang
            if 'golang' in ecds and len(ecds['golang']) > 0:
                # TODO: support multiple destinations
                dest = ecds['golang'][0]
                # TODO: support abs path
                rel_dest_fp = ecds['golang'][0]['relative_path']
                out_fp = (openapi_fp.parent / Path(rel_dest_fp)).resolve()
                assert out_fp.parent.exists()
                return out_fp

    if 'schemas' not in content['components']:
        return

    match = _match_volume(openapi_fp)
    if match is None:
        source_repr = remote.url_of_cache_path(openapi_fp) or str(openapi_fp)
        raise SystemExit(
            f'no volume matches source file {source_repr!r}; '
            f'pass --volume <src>:<dst> (src may be a local directory or a URL prefix) '
            f'to specify where its output should be written'
        )
    rel_source, output_dir = match
    return with_extension(output_dir / rel_source, '.go')



def write_file(output_fp: Path, content: str, header: str=CODEGEN_FILE_HEADER):
    GENERATED_OUT_FILES.append(output_fp)
    output_fp.parent.mkdir(parents=True, exist_ok=True)
    with output_fp.open('w') as f:
        f.write(header + '\n')
        f.write(f'package {output_fp.absolute().parent.name}\n\n')
        f.write(content)
        f.write('\n')


def parse_go_tags(tags: str) -> dict[str, str]:
    raise RuntimeError("fix bug: handle space inside tag value")
    res = {}
    for kv in tags.split():
        key, value = kv.split(':')
        value = value.strip('"')
        res[key] = value
    return res


def format_go_tags(tags: dict[str, str]) -> str:
    return ' '.join(map(lambda i: f'{i[0]}:"{i[1]}"', tags))


def merge_go_tags(raw_tags1: str, raw_tags2: str) -> str:
    tags1 = parse_go_tags(raw_tags1)
    tags2 = parse_go_tags(raw_tags2)
    tags_result = tags1
    for key, value in tags2.items():
        if key in tags_result:
            if key == 'validate':
                value = tags_result[key] + ' ' + value
        tags_result[key] = value
    return format_go_tags(tags_result)


class ProgrammingLanguage(str, Enum):
    Golang = 'golang'


class ModelField:
    class Type(str, Enum):
        Integer = 'integer'
        Number = 'number'
        Boolean = 'boolean'
        String = 'string'
        Array = 'array'
        Object = 'object'

    type: Type
    required: bool


class ObjectSchema:
    fields: ModelField


def _indent(lines: list[str], indent: str):
    return [indent + l for l in lines]


def _split_words(s: str) -> list[str]:
    words = []
    buf = ''
    for c in s:
        if c in ['_', '-']:
           words.append(buf)
           buf = ''
        elif c.isupper(): # in full caps string each letter will be a word
            words.append(buf)
            buf = c
        else:
            buf += c
    if len(buf) > 0:
        words.append(buf)
    words = [w for w in words if len(w) > 0]
    return words


def _camel_case(s: str) -> str:
    # hacks
    if s == 'ACCESS_TOKEN_LACKS_VAULT_TOKEN':
        return 'AccessTokenLacksVaultToken'

    result = ''.join(w.capitalize() for w in _split_words(s))
    # hack for abbreviations
    if 'Abc' in result:
        result = result.replace('Abc', 'ABC')
    if 'Vk' in result:
        result = result.replace('Vk', 'VK')
    if 'Acl' in result:
        result = result.replace('Acl', 'ACL')
    if 'Md5' in result:
        result = result.replace('Md5', 'MD5')
    if 'Yt' in result:
        result = result.replace('Yt', 'YT')
    if 'Ttl' in result:
        result = result.replace('Ttl', 'TTL')
    if 'Eq' == result:
        result = 'EQ'
    if 'Ge' == result:
        result = 'GE'
    if 'Le' == result:
        result = 'LE'
    return result


def _abbr(s: str) -> str:
    # hacks
    if s == 'SecretsSharingResponseCode':
        return 'SSRC'
    elif s == 'SecretsRemovalResponseCode':
        return 'SRemovalRC'
    elif s == 'SecretsRecoveryResponseCode':
        return 'SRecoveryRC'
    elif s == 'ExperimentsUpdateResponseCode':
        return 'EUpdateRC'
    elif s == 'ExperimentsStartResponseCode':
        return 'EStartRC'
    elif s == 'ExperimentsStopResponseCode':
        return 'EStopRC'

    return ''.join(filter(lambda c: c.isupper(), _camel_case(s)))


def _generate_golang_definition_object_fields(openapi_fp: Path, openapi_schema: dict) -> str:
    s = openapi_schema

    fields = []

    if 'allOf' in s:
        for subs in s['allOf']:
            if '$ref' in subs:
                ref_def, extra_tags = generate_golang_definition_ref(openapi_fp, subs['$ref'])
                go_def_override = prop_get(subs, 'go-def')
                if go_def_override is not None:
                    ref_def = go_def_override.rstrip('\n')
                fields.append(' '*4 + f'{ref_def} {extra_tags}')
                continue
            fields.extend(_generate_golang_definition_object_fields(openapi_fp, subs))
        return fields

    if '$ref' in s:
        ref_def, extra_tags = generate_golang_definition_ref(openapi_fp, s['$ref'])
        extra_tags_override = prop_get(s, 'go-extra-tags')
        if extra_tags_override is not None:
            # TODO: merge tags properly
            extra_tags = extra_tags_override + ' ' + extra_tags
        return [f'{ref_def} {extra_tags}']

    if 'properties' not in s:
        return ''

    required_props = set(s.get('required', []))
    for name, schema in s['properties'].items():
        prop_def = generate_golang_definition(openapi_fp, schema)
        ___extra_tags = None # super duper hack
        if hasattr(prop_def, '___extra_tags') and prop_def.___extra_tags != '':
            ___extra_tags = prop_def.___extra_tags
        is_required = name in required_props
        if not is_required:
            prop_def = _apply_optional_policy(prop_def, schema)
        field_name = prop_get(schema, 'go-field-name', _camel_case(name))
        prop_def_lines = prop_def.split('\n')
        prop_def_lines[0] = f'{field_name} {prop_def_lines[0]}'
        tags = f'json:"{name}"'
        field_extra_tags = prop_get(schema, 'go-extra-tags')
        if field_extra_tags is not None:
            tags += ' ' + field_extra_tags
        if ___extra_tags is not None:
            tags += ' ' + ___extra_tags
        prop_def_lines[-1] = f'{prop_def_lines[-1]} `{tags}`'
        prop_def_lines = _indent(prop_def_lines, ' '*4)
        prop_def = '\n'.join(prop_def_lines)
        fields.append(prop_def)
    return fields


def generate_golang_definition_object(openapi_fp: Path, openapi_schema: dict) -> str:
    s = openapi_schema
    go_def_override = prop_get(s, 'go-def')
    if go_def_override is not None:
        return go_def_override.rstrip('\n')
    if 'properties' not in openapi_schema and '$ref' not in openapi_schema and 'allOf' not in openapi_schema:
        value_type = 'interface{}'
        if 'additionalProperties' in s and isinstance(s['additionalProperties'], dict):
            if '$ref' in s['additionalProperties']:
                ref_def, _ = generate_golang_definition_ref(openapi_fp, s['additionalProperties']['$ref'])
                value_type = ref_def
            if 'type' in s['additionalProperties']:
                value_type = s['additionalProperties']['type']
        return f'map[string]{value_type}'
    lines = ['struct {']
    lines.extend(_generate_golang_definition_object_fields(openapi_fp, s))
    lines.append('}')
    return '\n'.join(lines)


def get_def_schema(openapi_fp: Path, def_path: list[str]) -> dict:
    with openapi_fp.open('r') as f:
        data = yaml.safe_load(f)
    node = data
    for p in def_path:
        node = node[p]
    return node

def _resolve_ref_target(openapi_fp: Path, ref_target: str) -> Path:
    """Resolve the file portion of a $ref relative to `openapi_fp`. The target
    may be a local relative path or an absolute http(s) URL. If `openapi_fp`
    itself originated from a URL and the target is a relative path, the
    target is resolved against the source URL."""
    if ref_target == '':
        return openapi_fp
    if remote.is_url(ref_target):
        return remote.fetch(ref_target)
    source_url = remote.url_of_cache_path(openapi_fp)
    if source_url is not None:
        joined = urllib.parse.urljoin(source_url, ref_target)
        return remote.fetch(joined)
    return (openapi_fp.parent / ref_target).resolve()


def _parse_ref(ref_value: str) -> tuple[str, list[str]]:
    """Split a $ref into (target, def_path_parts). Accepts both `target#/a/b/c`
    and the shorthand `target#Name` (treated as `components/schemas/Name`)."""
    if '#' not in ref_value:
        raise SystemExit(f'$ref missing fragment: {ref_value!r}')
    target, fragment = ref_value.split('#', 1)
    if fragment.startswith('/'):
        def_path = [p for p in fragment[1:].split('/') if p]
    else:
        def_path = ['components', 'schemas', fragment]
    return target, def_path


def generate_golang_definition_ref(openapi_fp: Path, ref_value: str) -> tuple[str, str]:
    ref_target, def_path = _parse_ref(ref_value)
    ref_fp = _resolve_ref_target(openapi_fp, ref_target)
    if ref_fp not in PROCESSED_FILES:
        PROCESSED_FILES.append(ref_fp)
        handle_definitions_file(ref_fp, get_out_fp(ref_fp), ProgrammingLanguage.Golang)

    ref_schema = get_def_schema(ref_fp, def_path)
    tags = ''
    ref_extra_tags = prop_get(ref_schema, 'go-extra-tags')
    if ref_extra_tags is not None:
        tags += ' ' + ref_extra_tags

    def_name = prop_get(ref_schema, 'go-def-name', _camel_case(def_path[-1]))
    go_rel_def_name = ''
    # TODO: compare output file paths
    if openapi_fp.resolve().parent == ref_fp.resolve().parent:
        go_rel_def_name = def_name
    else:
        go_rel_def_name = f'{get_out_fp(ref_fp).parent.name}.{def_name}'
    return go_rel_def_name, tags


class GoDef(str):
    ___extra_tags: str

def generate_golang_definition(openapi_fp: Path, openapi_schema: dict) -> GoDef:
    s = openapi_schema

    go_def_override = prop_get(s, 'go-def')
    if go_def_override is not None:
        return go_def_override.rstrip('\n')

    if '$ref' in s:
        ref_def, extra_tags = generate_golang_definition_ref(openapi_fp, s['$ref'])
        ref_def = GoDef(ref_def)
        ref_def.___extra_tags = extra_tags
        return ref_def

    match s['type']:
        case 'integer':
            prefix = '*' if s.get('nullable', False) else ''
            return prefix+'int64'
        case 'number':
            prefix = '*' if s.get('nullable', False) else ''
            return 'float64'
        case 'boolean':
            prefix = '*' if s.get('nullable', False) else ''
            return 'bool'
        case 'string':
            prefix = '*' if s.get('nullable', False) else ''
            return 'string'
        case 'array':
            items_schema = s['items']
            return f'[]{generate_golang_definition(openapi_fp, items_schema)}'
        case 'object':
            return generate_golang_definition_object(openapi_fp, s)
    raise RuntimeError(f'Got unsupported openapi definition object type: {s["type"]}')


def generate_definition(openapi_fp: Path, openapi_schema: dict, output_lang: ProgrammingLanguage) -> str:
    return generate_golang_definition(openapi_fp, openapi_schema)

def handle_definitions_file(openapi_fp: Path, output_fp: Path, output_lang: ProgrammingLanguage):
    assert openapi_fp.exists()
    assert openapi_fp.is_file()
    assert openapi_fp.suffix == '.yaml'
    with openapi_fp.open('r') as f:
        content = yaml.safe_load(f)
    if 'components' not in content:
        return
    if 'schemas' not in content['components']:
        return

    defs = []
    vars_groups = []
    for name, schema in content['components']['schemas'].items():
        # if '$ref' not in schema and schema['type'] != 'object':
        #     continue
        try:
            golang_def = generate_definition(openapi_fp, schema, output_lang)
        except Exception as e:
            raise RuntimeError(f'Failed to generate definition for schema {name} ({openapi_fp}) : {e}')
        golang_def_lines = golang_def.split('\n')
        golang_def_name = _camel_case(name)
        golang_def_lines[0] = f'type {golang_def_name} {golang_def_lines[0]}'
        golang_def = '\n'.join(golang_def_lines)
        defs.append(golang_def)

        # data generation (enum values)
        if 'enum' in schema:
            enum_type = golang_def_name
            enum_type_abbrev = prop_get(schema, 'go-type-name-abbrev', _abbr(enum_type))

            enum_data_gen_options = prop_get(schema, 'enum-data-gen', ['values'])
            enum_data_gen_name = prop_canonical('enum-data-gen')
            if not isinstance(enum_data_gen_options, list):
                raise RuntimeError(f'{enum_data_gen_name} must be a list, got: {type(enum_data_gen_options)}')

            # Validate options
            valid_options = {'values', 'aggregated_values'}
            for opt in enum_data_gen_options:
                if opt not in valid_options:
                    raise RuntimeError(f'Invalid {enum_data_gen_name} option: {opt}. Valid options: {valid_options}')

            # Generate individual enum values
            if 'values' in enum_data_gen_options:
                vars_lines = list(map(lambda v: f'{enum_type_abbrev}_{_camel_case(v)} {enum_type} = "{v}"', schema['enum']))
                vars_lines = ['var ('] + vars_lines + [')']
                vars_groups.append('\n'.join(vars_lines))

            # Generate aggregated values slice
            if 'aggregated_values' in enum_data_gen_options:
                enum_const_names = [f'{enum_type_abbrev}_{_camel_case(v)}' for v in schema['enum']]
                aggregated_var_name = f'{enum_type}_AllValues'
                aggregated_lines = [
                    f'var {aggregated_var_name} = []{enum_type}{{',
                ] + [f'\t{name},' for name in enum_const_names] + ['}']
                vars_groups.append('\n'.join(aggregated_lines))

    source_url = remote.url_of_cache_path(openapi_fp)
    if source_url is not None:
        header = CODEGEN_FILE_HEADER + f' (source: {source_url})'
    else:
        header = CODEGEN_FILE_HEADER + f' (source: {openapi_fp.absolute().relative_to(_find_git_root_fp(openapi_fp))})'
    write_file(output_fp, '\n\n'.join(defs+ vars_groups), header=header)


def prettify_uri_part(parts: list[str], i: int) -> str:
    def truncate_repetition(src: str, target: str, depth=0):
        if src.startswith(target):
            return src.replace(target, '')
        elif (target[-1] == 's' and src.startswith(target[:-1])):
            return src.replace(target[:-1], '')
        elif depth == 0 and '_' in src:
            return truncate_repetition(src.replace('_', '-'), target, depth+1)
        elif depth == 0 and '-' in src:
            return truncate_repetition(src.replace('-', '_'), target, depth+1)
        return src

    part = parts[i]
    if part.startswith('{') and part.endswith('}'):
        part = part.strip('{}')
        if i-1 >= 0:
            prev_part = parts[i-1]
            part = truncate_repetition(part, prev_part)
    return part

def _apply_optional_policy(cur_def, schema):
    optional_policy = prop_get(schema, 'go-null-or-undefined-is', 'any_nil')
    if optional_policy == 'any_nil':
        if not (cur_def.startswith('[]') or cur_def.startswith('map[')):
            cur_def = '*' + cur_def
    elif optional_policy == 'default':
        pass
    elif optional_policy == 'nil_pointer':
        cur_def = '*' + cur_def
    else:
        raise RuntimeError(f'Got unexpected optional value policy: {optional_policy}')
    return cur_def

def handle_api_file(openapi_api_fp: Path, output_dir: Path, output_lang: ProgrammingLanguage):
    # TODO: if no fields in response body -> use shortcut
    # TODO: support additionalProperties types


    assert openapi_api_fp.exists()
    assert openapi_api_fp.is_file()
    assert openapi_api_fp.suffix == '.yaml'
    with openapi_api_fp.open('r') as f:
        content = yaml.safe_load(f)

    for path in content['paths']:
        canonized_path_parts = []
        parts = path.split('/')
        for i in range(len(parts)):
            part = prettify_uri_part(parts, i)
            part = part.replace('_', '').replace('-', '')
            canonized_path_parts.append(part)
        canonized_path = '/'.join(canonized_path_parts)
        cano_path_camel_case = ''.join(w.capitalize() for w in canonized_path.split('/'))

        for method in content['paths'][path]:
            handler_schema = content['paths'][path][method]
            uri_fields = []
            query_fields = []
            # uri_name2type = dict()
            uri_name2field_name = dict()
            req_has_headers = False
            for p in handler_schema.get('parameters', []):
                match p['in']:
                    case 'path':
                        name = p['name']
                        p_def = generate_golang_definition(openapi_api_fp, p['schema'])
                        is_required = 'required' in p and p['required'] is True
                        if not is_required and not (p_def.startswith('[]') or p_def.startswith('map[')):
                            p_def = _apply_optional_policy(p_def, p)
                        tags = f'uri:"{name}"'
                        uri_extra_tags = prop_get(p['schema'], 'go-extra-tags')
                        if uri_extra_tags is not None:
                            tags += ' ' + uri_extra_tags
                        field_name = prop_get(p, 'go-field-name', _camel_case(name))
                        p_def = f'{field_name} {p_def} `{tags}`'
                        uri_fields.append(p_def)

                        # uri_name2type[p['name']] = p_def
                        uri_name2field_name[p['name']] = field_name
                    case 'query':
                        name = p['name']
                        p_def = generate_golang_definition(openapi_api_fp, p['schema'])
                        is_required = 'required' in p and p['required'] is True
                        # try to update
                        # if not is_required and not (p_def.startswith('[]') or p_def.startswith('map[')):
                        if not is_required:
                            p_def = _apply_optional_policy(p_def, p)
                        tags = f'query:"{name}" form:"{name}"'
                        query_extra_tags = prop_get(p['schema'], 'go-extra-tags')
                        if query_extra_tags is not None:
                            tags += ' ' + query_extra_tags
                        field_name = prop_get(p, 'go-field-name', _camel_case(name))
                        p_def = f'{field_name} {p_def} `{tags}`'
                        query_fields.append(p_def)
                    case 'header':
                        req_has_headers = True
                    case _:
                        raise RuntimeError(f'Got unsupported parameter in handler schema: {p}')

            uri_code = ''
            if len(uri_fields) > 0:
                uri_code_lines = []
                uri_code_lines.append('fw.RequestURI[struct {')
                uri_code_lines.extend(_indent(uri_fields,' '*4))
                uri_code_lines.append('}]')
                uri_code = '\n'.join(uri_code_lines)
            else:
                uri_code = 'fw.RequestNoURI'

            query_code = ''
            if len(query_fields) > 0:
                query_code_lines = []
                query_code_lines.append('fw.RequestQuery[struct {')
                query_code_lines.extend(_indent(query_fields,' '*8))
                query_code_lines.append(' '*4 + '}]')
                query_code = '\n'.join(query_code_lines)
            else:
                query_code = 'fw.RequestNoQuery'

            req_body_code = ''
            if method.upper() == 'GET' or 'requestBody' not in handler_schema:
                req_body_code = 'fw.RequestNoBody'
            else:
                content_types = set(ct for ct in handler_schema['requestBody']['content'])
                if 'application/json' in content_types:
                    request_body_schema = handler_schema['requestBody']['content']['application/json']['schema']
                    req_body_def = generate_golang_definition_object(openapi_api_fp, request_body_schema)
                    req_body_code = ' '*4 + f'fw.RequestBodyJSON[{req_body_def}]'
                elif 'application/octet-stream' in content_types:
                   req_body_code = ' '*4 + f'fw.RequestBodyStream'
                else:
                    raise RuntimeError(f'Got unsupported content types: {content_types}')

            request_class_name = f'{cano_path_camel_case}{method.upper()}Request'
            path_expr = ''
            if len(uri_fields) == 0:
                path_expr = f'"{path}"'
            else:
                new_parts = []
                uri_names = []
                for part in path.split('/'):
                    if part.startswith('{') and part.endswith('}'):
                        uri_name = part.strip('{}')
                        uri_names.append(uri_name)
                        part = '%v'
                        # uri_field_type = uri_name2type[uri_name]
                        # match uri_field_type:
                        #     case 'integer':
                        #         part = '%d'
                        #     case 'string':
                        #         part = '%s'
                        #     case _:
                        #         raise RuntimeError(f'Got unsupported uri field type: {uri_field_type}')
                    new_parts.append(part)
                path_format = '/'.join(new_parts)
                args = ', '.join(f'r.URI.{uri_name2field_name[name]}' for name in uri_names)
                path_expr = f'fmt.Sprintf("{path_format}", {args})'

            resp_code = ''
            for status_code in handler_schema['responses']:
                resp_schema = handler_schema['responses'][status_code]
                has_headers = 'headers' in resp_schema

                if 'content' in resp_schema:
                    content_types = set(ct for ct in resp_schema['content'])
                    resp_class_name = f'{cano_path_camel_case}{method.upper()}Response{status_code}'
                    if 'application/json' in content_types:
                        resp_body_schema = resp_schema['content']['application/json']['schema']
                        resp_body_def = generate_golang_definition_object(openapi_api_fp, resp_body_schema)
                        resp_body_code = ' '*4 + f'fw.ResponseBodyJSON[{resp_body_def}]'
                    elif 'application/octet-stream' in content_types:
                        resp_body_code = ' '*4 + f'fw.ResponseBodyStream'
                    else:
                        raise RuntimeError(f'Got unsupported content types: {content_types}')


                    resp_code += '\n'.join([
                        f'type {resp_class_name} struct {{',
                        ' '*4 + f'fw.Response{status_code}',
                        ' '*4 + f'fw.ResponseHeader[fw.JSONHeaderPreset]',
                        '',
                        resp_body_code,
                        '}',
                        '',
                    ])
                else:
                    resp_code += f'type {cano_path_camel_case}{method.upper()}Response{status_code} = fw.Response{status_code}WithJSONHeader\n'

            code = '\n'.join([
                f'import "{FW_IMPORT_PATH}"',
                '',
                f'type {request_class_name} struct {{',
                ' '*4 + f'fw.Request{method.upper()}',
                ' '*4 + f'fw.RequestPathBound',
                ' '*4 + f'fw.RequestHeader[fw.JSONHeaderPreset]',
                '',
                ' '*4 + uri_code,
                ' '*4 + query_code,
                ' '*4 + req_body_code,
                '}',
                '',
                f'func (r *{request_class_name}) Path() string {{',
                ''*4 + f'return {path_expr}',
                '}',
                '',
                resp_code,
            ])

            output_file_name = CODEGEN_FILE_NAME_PREFIX + canonized_path.strip('/').replace('/', '_')  + '_' + method.upper() + CODEGEN_FILE_NAME_SUFFIX + '.go'
            output_fp = (output_dir / output_file_name)
            write_file(output_fp, code)

