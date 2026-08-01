import pytest
from app.services.sql_guard import validate_select, SQLGuardError

def test_validate_select_valid():
    sql = "SELECT * FROM tasks WHERE status = 'todo'"
    guarded = validate_select(sql)
    assert guarded == "SELECT * FROM (SELECT * FROM tasks WHERE status = 'todo') q LIMIT 501"
    
    sql2 = "WITH t AS (SELECT id FROM tasks) SELECT * FROM t"
    guarded2 = validate_select(sql2)
    assert guarded2 == "SELECT * FROM (WITH t AS (SELECT id FROM tasks) SELECT * FROM t) q LIMIT 501"
    
    sql3 = "SELECT project, count(*) FROM tasks GROUP BY project"
    guarded3 = validate_select(sql3)
    assert guarded3 == "SELECT * FROM (SELECT project, count(*) FROM tasks GROUP BY project) q LIMIT 501"

def test_validate_select_invalid_statements():
    with pytest.raises(SQLGuardError, match="Only SELECT statements are allowed"):
        validate_select("UPDATE tasks SET status='done'")
        
    with pytest.raises(SQLGuardError, match="Only SELECT statements are allowed"):
        validate_select("INSERT INTO tasks (id) VALUES ('t1')")
        
    with pytest.raises(SQLGuardError, match="Only SELECT statements are allowed"):
        validate_select("DELETE FROM tasks")
        
    with pytest.raises(SQLGuardError, match="SELECT INTO is not allowed"):
        validate_select("SELECT 1 INTO new_table")

    with pytest.raises(SQLGuardError, match="Only SELECT statements are allowed in CTEs"):
        validate_select("WITH x AS (DELETE FROM tasks) SELECT * FROM x")

    with pytest.raises(SQLGuardError, match="SELECT FOR UPDATE/SHARE is not allowed"):
        validate_select("SELECT * FROM tasks FOR UPDATE")
        
    with pytest.raises(SQLGuardError, match="Only SELECT statements are allowed"):
        validate_select("COMMIT")
        
    with pytest.raises(SQLGuardError, match="Only SELECT statements are allowed"):
        validate_select("SET statement_timeout = 0")

def test_validate_select_multiple_statements():
    with pytest.raises(SQLGuardError, match="Only a single SQL statement is allowed"):
        validate_select("SELECT * FROM tasks; SELECT * FROM projects")

def test_validate_select_banned_functions():
    with pytest.raises(SQLGuardError, match="Function pg_sleep is not allowed"):
        validate_select("SELECT pg_sleep(10)")
        
    with pytest.raises(SQLGuardError, match="Function dblink is not allowed"):
        validate_select("SELECT * FROM dblink('host=localhost', 'SELECT 1') AS t(id int)")

def test_validate_select_syntax_error():
    with pytest.raises(SQLGuardError, match="Invalid SQL"):
        validate_select("SELECT * FRO tasks")
