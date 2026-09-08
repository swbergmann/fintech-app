#!/bin/bash
set -euo pipefail
# Probe the actual PDB, independently of image-specific startup hooks and paths.
sqlplus -L -s / as sysdba <<'SQL'
WHENEVER OSERROR EXIT FAILURE
WHENEVER SQLERROR EXIT FAILURE
SET HEADING OFF FEEDBACK OFF
ALTER SESSION SET CONTAINER = FREEPDB1;
DECLARE
    pdb_mode VARCHAR2(20);
BEGIN
    SELECT open_mode INTO pdb_mode FROM v$pdbs WHERE name = 'FREEPDB1';
    IF pdb_mode <> 'READ WRITE' THEN
        RAISE_APPLICATION_ERROR(-20001, 'FREEPDB1 is not open read write');
    END IF;
END;
/
SELECT 1 FROM dual;
EXIT SUCCESS;
SQL
