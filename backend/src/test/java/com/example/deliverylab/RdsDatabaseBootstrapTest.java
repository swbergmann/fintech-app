package com.example.deliverylab;

import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.HashMap;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class RdsDatabaseBootstrapTest {
    private static final String PASSWORD = "Lab9FictionalPassword123456789";
    private final Connection admin = mock(Connection.class);
    private final Connection app = mock(Connection.class);
    private final Statement ddl = mock(Statement.class);
    private final CallableStatement createUser = mock(CallableStatement.class);
    private final RdsDatabaseBootstrap.Connector connector = mock(RdsDatabaseBootstrap.Connector.class);

    private Map<String, String> environment() {
        return new HashMap<>(Map.of("DB_URL", "jdbc:oracle:thin:@//example:1521/LAB",
            "DB_ADMIN_USERNAME", "LABADMIN", "DB_ADMIN_PASSWORD", "FictionalAdminPassword",
            "DB_USERNAME", "APP", "DB_PASSWORD", PASSWORD));
    }

    private ResultSet result(int value) throws SQLException {
        var result = mock(ResultSet.class);
        when(result.next()).thenReturn(true);
        when(result.getInt(1)).thenReturn(value);
        return result;
    }

    @BeforeEach void configureConnections() throws SQLException {
        when(connector.connect(anyString(), eq("LABADMIN"), anyString())).thenReturn(admin);
        when(connector.connect(anyString(), eq("APP"), anyString())).thenReturn(app);
        when(admin.createStatement()).thenReturn(ddl);
        when(admin.prepareCall(anyString())).thenReturn(createUser);
        var check = mock(Statement.class);
        when(app.createStatement()).thenReturn(check);
        doReturn(result(1)).when(check).executeQuery("SELECT 1 FROM dual");
        doReturn(result(0)).when(ddl).executeQuery(contains("dba_users"));
        doReturn(result(0)).when(ddl).executeQuery(contains("dba_tablespaces"));
    }

    @Test void createsBoundedTablespaceAndAppUserWithoutLocalFilesOrSysdba() throws SQLException {
        RdsDatabaseBootstrap.bootstrap(RdsDatabaseBootstrap.settings(environment()), connector);
        verify(ddl).executeUpdate("CREATE TABLESPACE APP_DATA DATAFILE SIZE 20M AUTOEXTEND ON NEXT 10M MAXSIZE 100M");
        verify(createUser).setString(1, PASSWORD);
        verify(createUser).execute();
        verify(ddl).executeUpdate("GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE TO APP");
        verify(connector).connect(anyString(), eq("APP"), eq(PASSWORD));
    }

    @Test void rerunPreservesExistingUserPasswordAndTables() throws SQLException {
        doReturn(result(1)).when(ddl).executeQuery(contains("dba_users"));
        doReturn(result(1)).when(ddl).executeQuery(contains("dba_tablespaces"));
        RdsDatabaseBootstrap.bootstrap(RdsDatabaseBootstrap.settings(environment()), connector);
        verify(admin, never()).prepareCall(anyString());
        verify(ddl, never()).executeUpdate(startsWith("CREATE"));
        verify(ddl, never()).executeUpdate(contains("IDENTIFIED BY"));
        verify(connector, times(2)).connect(anyString(), eq("APP"), eq(PASSWORD));
    }

    @Test void mismatchedExistingPasswordFailsBeforeDdlWithoutLeakingCredentials() throws SQLException {
        doReturn(result(1)).when(ddl).executeQuery(contains("dba_users"));
        when(connector.connect(anyString(), eq("APP"), anyString()))
            .thenThrow(new SQLException("driver message with " + PASSWORD, "72000", 1017));
        var error = assertThrows(IllegalStateException.class,
            () -> RdsDatabaseBootstrap.bootstrap(RdsDatabaseBootstrap.settings(environment()), connector));
        assertTrue(error.getMessage().contains("1017"));
        assertFalse(error.getMessage().contains(PASSWORD));
        assertNull(error.getCause());
        verify(ddl, never()).executeUpdate(anyString());
        verify(admin, never()).prepareCall(anyString());
    }

    @Test void ddlFailureHidesSqlAndPasswordAndStopsBootstrap() throws SQLException {
        when(createUser.execute()).thenThrow(new SQLException("CREATE USER with " + PASSWORD, "72000", 28003));
        var error = assertThrows(IllegalStateException.class,
            () -> RdsDatabaseBootstrap.bootstrap(RdsDatabaseBootstrap.settings(environment()), connector));
        assertFalse(error.toString().contains(PASSWORD));
        assertNull(error.getCause());
        verify(ddl, never()).executeUpdate(startsWith("GRANT"));
    }

    @Test void rejectsUnsafePasswordsAndNonLabUsers() {
        for (String password : new String[] {"short", "x".repeat(20) + "\"", "x".repeat(20) + "\n", "x".repeat(31), "x".repeat(32)}) {
            var env = environment();
            env.put("DB_PASSWORD", password);
            assertThrows(IllegalArgumentException.class, () -> RdsDatabaseBootstrap.settings(env));
        }
        for (String user : new String[] {"SYS", "SYSTEM", "APP"}) {
            var env = environment();
            env.put("DB_ADMIN_USERNAME", user);
            assertThrows(IllegalArgumentException.class, () -> RdsDatabaseBootstrap.settings(env));
        }
    }

    @Test void rejectsMissingCredentialsAndOtherJdbcDrivers() {
        var env = environment();
        env.remove("DB_ADMIN_PASSWORD");
        assertThrows(IllegalArgumentException.class, () -> RdsDatabaseBootstrap.settings(env));
        env.put("DB_ADMIN_PASSWORD", "FictionalAdminPassword");
        env.put("DB_URL", "jdbc:h2:mem:lab");
        assertThrows(IllegalArgumentException.class, () -> RdsDatabaseBootstrap.settings(env));
    }

    @Test void settingsToStringRedactsPasswords() {
        var settings = RdsDatabaseBootstrap.settings(environment());
        assertFalse(settings.toString().contains(PASSWORD));
        assertFalse(settings.toString().contains("FictionalAdminPassword"));
    }
}
