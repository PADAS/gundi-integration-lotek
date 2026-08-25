"""A minimal Lua-subset interpreter for the two hand-rolled Redis Lua scripts
in app/services/state.py (_INCREMENT_COUNTER_SCRIPT, _ACQUIRE_LEASE_SCRIPT).

Neither lupa nor fakeredis[lua] is available here without adding a new
project dependency (see test_state_manager.py for why that path was ruled
out), so this parses and executes the *actual* script text -- not a
hand-copied transliteration of it -- against a tiny fake Redis. It covers
exactly the handful of Lua constructs these two scripts use: `local`
declarations, a single level of `if ... then ... [else ...] end`, `and`,
`==`/`~=`, table indexing (KEYS[n] / ARGV[n] / table.field), and calls into
redis.call / redis.pcall / redis.error_reply / string.find / string.lower.
Because expressions are genuinely parsed and evaluated (not pattern-matched
against known-good text), a change anywhere in the script -- not just a
specific anticipated mutation -- changes what this actually executes.
"""
import re


class LuaError(Exception):
    """Raised by a fake redis.call failure (mirrors a real Redis error
    aborting an unprotected `redis.call`)."""


class _ErrorTable:
    """What `redis.pcall`/`redis.error_reply` produce on failure: a Lua
    table with an `err` field, distinguishable from plain values by type()."""

    def __init__(self, err):
        self.err = err


class _Return(Exception):
    def __init__(self, value):
        self.value = value


# ---------------------------------------------------------------- tokenizer
_TOKEN_RE = re.compile(
    r"""
    (?P<SKIP>\s+)
  | (?P<STRING>'[^']*')
  | (?P<NUMBER>\d+)
  | (?P<NAME>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<EQ>==)
  | (?P<NE>~=)
  | (?P<PUNCT>[()\[\].,=])
    """,
    re.VERBOSE,
)


class _Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value

    def __repr__(self):
        return f"_Token({self.kind!r}, {self.value!r})"


def _tokenize(src):
    tokens = []
    pos = 0
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m:
            raise SyntaxError(f"cannot tokenize at {src[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "SKIP":
            continue
        tokens.append(_Token(kind, m.group(kind)))
    tokens.append(_Token("EOF", None))
    return tokens


# -------------------------------------------------------------------- parser
class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def advance(self):
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def expect_name(self, value):
        t = self.advance()
        if t.kind != "NAME" or t.value != value:
            raise SyntaxError(f"expected {value!r}, got {t.value!r}")

    def expect_punct(self, value):
        t = self.advance()
        if t.value != value:
            raise SyntaxError(f"expected {value!r}, got {t.value!r}")

    def at_block_end(self):
        t = self.peek()
        return t.kind == "EOF" or (t.kind == "NAME" and t.value in ("end", "else"))

    def parse_block(self):
        stmts = []
        while not self.at_block_end():
            stmts.append(self.parse_statement())
        return stmts

    def parse_statement(self):
        t = self.peek()
        if t.kind == "NAME" and t.value == "local":
            self.advance()
            name = self.advance().value
            self.expect_punct("=")
            expr = self.parse_expr()
            return ("local", name, expr)
        if t.kind == "NAME" and t.value == "if":
            self.advance()
            cond = self.parse_expr()
            self.expect_name("then")
            then_block = self.parse_block()
            else_block = []
            if self.peek().kind == "NAME" and self.peek().value == "else":
                self.advance()
                else_block = self.parse_block()
            self.expect_name("end")
            return ("if", cond, then_block, else_block)
        if t.kind == "NAME" and t.value == "return":
            self.advance()
            expr = self.parse_expr()
            return ("return", expr)
        expr = self.parse_expr()
        if self.peek().value == "=":
            self.advance()
            rhs = self.parse_expr()
            if expr[0] != "var":
                raise SyntaxError("assignment target must be a plain name")
            return ("assign", expr[1], rhs)
        return ("exprstmt", expr)

    def parse_expr(self):
        left = self.parse_equality()
        while self.peek().kind == "NAME" and self.peek().value == "and":
            self.advance()
            right = self.parse_equality()
            left = ("and", left, right)
        return left

    def parse_equality(self):
        left = self.parse_postfix()
        while self.peek().kind in ("EQ", "NE"):
            op = self.advance().kind
            right = self.parse_postfix()
            left = ("eq" if op == "EQ" else "ne", left, right)
        return left

    def parse_postfix(self):
        expr = self.parse_primary()
        while True:
            t = self.peek()
            if t.value == ".":
                self.advance()
                name = self.advance().value
                expr = ("attr", expr, name)
            elif t.value == "[":
                self.advance()
                idx = self.parse_expr()
                self.expect_punct("]")
                expr = ("index", expr, idx)
            elif t.value == "(":
                self.advance()
                args = []
                if self.peek().value != ")":
                    args.append(self.parse_expr())
                    while self.peek().value == ",":
                        self.advance()
                        args.append(self.parse_expr())
                self.expect_punct(")")
                expr = ("call", expr, args)
            else:
                break
        return expr

    def parse_primary(self):
        t = self.advance()
        if t.kind == "STRING":
            return ("lit", t.value[1:-1])
        if t.kind == "NUMBER":
            return ("lit", int(t.value))
        if t.kind == "NAME":
            if t.value == "nil":
                return ("lit", None)
            if t.value == "false":
                return ("lit", False)
            if t.value == "true":
                return ("lit", True)
            return ("var", t.value)
        if t.value == "(":
            expr = self.parse_expr()
            self.expect_punct(")")
            return expr
        raise SyntaxError(f"unexpected token {t.value!r}")


# ---------------------------------------------------------------- evaluator
def _truthy(v):
    return v is not None and v is not False


def _lua_type(v):
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, _ErrorTable):
        return "table"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (int, float)):
        return "number"
    if v is None:
        return "nil"
    return "userdata"


class _StringLib:
    @staticmethod
    def find(haystack, needle):
        idx = haystack.find(needle)
        return None if idx == -1 else idx + 1  # Lua is 1-indexed

    @staticmethod
    def lower(s):
        return s.lower()


def _eval(node, env):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "var":
        name = node[1]
        if name in env["locals"]:
            return env["locals"][name]
        if name in env["globals"]:
            return env["globals"][name]
        raise NameError(f"undefined name {name!r}")
    if kind == "attr":
        obj = _eval(node[1], env)
        return getattr(obj, node[2], None)
    if kind == "index":
        obj = _eval(node[1], env)
        idx = _eval(node[2], env)
        return obj[idx - 1]
    if kind == "call":
        func = _eval(node[1], env)
        args = [_eval(a, env) for a in node[2]]
        return func(*args)
    if kind == "and":
        left = _eval(node[1], env)
        return left if not _truthy(left) else _eval(node[2], env)
    if kind == "eq":
        return _eval(node[1], env) == _eval(node[2], env)
    if kind == "ne":
        return _eval(node[1], env) != _eval(node[2], env)
    raise SyntaxError(f"cannot eval {node!r}")


def _exec_block(block, env):
    for stmt in block:
        _exec_stmt(stmt, env)


def _exec_stmt(stmt, env):
    kind = stmt[0]
    if kind == "local" or kind == "assign":
        env["locals"][stmt[1]] = _eval(stmt[2], env)
    elif kind == "if":
        _, cond, then_block, else_block = stmt
        _exec_block(then_block if _truthy(_eval(cond, env)) else else_block, env)
    elif kind == "return":
        raise _Return(_eval(stmt[1], env))
    elif kind == "exprstmt":
        _eval(stmt[1], env)
    else:
        raise SyntaxError(f"cannot exec {stmt!r}")


# ------------------------------------------------------------------ fake redis
class FakeRedis:
    """Fake Redis command table for these two scripts. GET returns Lua
    `false` (not `nil`) for a missing key, matching redis-lua's conversion,
    which is why both scripts compare against `false` rather than `nil`."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    def call(self, cmd, *args):
        return self._dispatch(cmd, args)

    def pcall(self, cmd, *args):
        try:
            return self._dispatch(cmd, args)
        except LuaError as exc:
            return _ErrorTable(str(exc))

    def error_reply(self, msg):
        return _ErrorTable(msg)

    def _dispatch(self, cmd, args):
        cmd = cmd.upper()
        key = args[0]
        if cmd == "GET":
            return self.store.get(key, False)
        if cmd == "SET":
            value = args[1]
            self.store[key] = value
            if len(args) >= 4 and args[2] == "EX":
                self.ttls[key] = args[3]
            else:
                self.ttls.pop(key, None)
            return "OK"
        if cmd == "DEL":
            existed = key in self.store
            self.store.pop(key, None)
            self.ttls.pop(key, None)
            return 1 if existed else 0
        if cmd == "EXPIRE":
            self.ttls[key] = args[1]
            return 1
        if cmd == "INCR":
            current = self.store.get(key)
            if current is None:
                new = 1
            else:
                try:
                    new = int(current) + 1
                except (TypeError, ValueError):
                    raise LuaError("value is not an integer or out of range")
            self.store[key] = new
            return new
        raise LuaError(f"unsupported command {cmd}")


def run_script(script, redis_stub, keys, argv):
    """Parse and execute `script` (the real Lua text) against `redis_stub`,
    returning whatever the script `return`s. A script that returns an error
    table (via `redis.error_reply`, or an uncaught `redis.call` failure)
    raises LuaError -- mirroring redis-py raising ResponseError for a script
    that returns/raises an error."""
    block = _Parser(_tokenize(script)).parse_block()
    env = {
        "locals": {},
        "globals": {
            "KEYS": keys,
            "ARGV": argv,
            "redis": redis_stub,
            "string": _StringLib(),
            "type": _lua_type,
        },
    }
    try:
        _exec_block(block, env)
    except _Return as r:
        value = r.value
        if isinstance(value, _ErrorTable):
            raise LuaError(value.err)
        return value
    return None
