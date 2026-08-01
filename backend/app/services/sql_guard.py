import pglast
from pglast.ast import SelectStmt, FuncCall, Node

# Functions that might have side-effects or wait indefinitely
BANNED_FUNCTIONS = {
    'pg_sleep',
    'pg_sleep_for',
    'pg_sleep_until',
    'dblink',
    'dblink_exec',
    'dblink_connect',
    'dblink_disconnect',
    'pg_advisory_lock',
    'pg_advisory_unlock',
}

class SQLGuardError(Exception):
    pass

def _check_node_for_banned_functions(node):
    if isinstance(node, FuncCall):
        # node.funcname is a list of String objects
        func_name = ".".join([n.sval for n in node.funcname if hasattr(n, 'sval')]).lower()
        if func_name in BANNED_FUNCTIONS or func_name.split('.')[-1] in BANNED_FUNCTIONS:
            raise SQLGuardError(f"Function {func_name} is not allowed")
    
    # Recursively check children
    if isinstance(node, tuple) or isinstance(node, list):
        for child in node:
            _check_node_for_banned_functions(child)
    elif isinstance(node, Node):
        for name in node:
            val = getattr(node, name, None)
            if val is not None:
                _check_node_for_banned_functions(val)

def _validate_select_stmt(stmt):
    if not isinstance(stmt, SelectStmt):
        raise SQLGuardError("Only SELECT statements are allowed in CTEs")
    
    if getattr(stmt, 'intoClause', None) is not None:
        raise SQLGuardError("SELECT INTO is not allowed")
    
    if getattr(stmt, 'lockingClause', None) is not None and len(stmt.lockingClause) > 0:
        raise SQLGuardError("SELECT FOR UPDATE/SHARE is not allowed")

    if getattr(stmt, 'withClause', None) is not None:
        for cte in stmt.withClause.ctes:
            _validate_select_stmt(cte.ctequery)

def validate_select(sql: str) -> str:
    """
    Validates that the given SQL is a single, safe SELECT or WITH statement.
    Wraps it in a row-cap LIMIT 501.
    """
    try:
        # Parse the SQL
        ast = pglast.parse_sql(sql)
    except pglast.parser.ParseError as e:
        raise SQLGuardError(f"Invalid SQL: {e}")

    # Must be a single statement
    if len(ast) != 1:
        raise SQLGuardError("Only a single SQL statement is allowed")

    stmt = ast[0].stmt
    
    # Must be a SELECT statement
    if not isinstance(stmt, SelectStmt):
        raise SQLGuardError("Only SELECT statements are allowed")

    _validate_select_stmt(stmt)

    # The statement might contain banned function calls
    _check_node_for_banned_functions(stmt)

    # Wrap the query in a row cap limit
    clean_sql = sql.strip().rstrip(';')
    
    return f"SELECT * FROM ({clean_sql}) q LIMIT 501"
