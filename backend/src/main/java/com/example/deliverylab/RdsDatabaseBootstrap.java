package com.example.deliverylab;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.Map;

/** Explicit one-off RDS setup; never runs during normal application startup. */
public final class RdsDatabaseBootstrap {
    private RdsDatabaseBootstrap() {}

    record Settings(String url, String adminUser, String adminPassword, String appPassword) {
        // Credentials must not leak through logging or an exception's record representation.
        @Override public String toString() { return "RdsBootstrapSettings[redacted]"; }
    }

    @FunctionalInterface
    interface Connector {
        Connection connect(String url, String user, String password) throws SQLException;
    }

    static Settings settings(Map<String, String> env) {
        String url = required(env, "DB_URL");
        if (!url.startsWith("jdbc:oracle:thin:@//") || url.contains("\n") || url.contains("\r")) {
            throw new IllegalArgumentException("DB_URL must be an Oracle Thin service URL.");
        }
        String adminUser = required(env, "DB_ADMIN_USERNAME");
        if (!"LABADMIN".equals(adminUser) || !"APP".equals(required(env, "DB_USERNAME"))) {
            throw new IllegalArgumentException("Bootstrap requires the lab's LABADMIN and APP users.");
        }
        String password = required(env, "DB_PASSWORD");
        // The PL/SQL block binds a value, then uses it in Oracle's dynamic DDL.
        // Match the generated secret before it can reach that quoted identifier.
        if (!password.matches("[A-Za-z0-9]{20,30}")) {
            throw new IllegalArgumentException("APP password must contain 20–30 letters and digits for Oracle 19c.");
        }
        return new Settings(url, adminUser, required(env, "DB_ADMIN_PASSWORD"), password);
    }

    private static String required(Map<String, String> env, String name) {
        String value = env.get(name);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Missing bootstrap setting: " + name);
        }
        return value;
    }

    public static void run(Map<String, String> env) {
        bootstrap(settings(env), DriverManager::getConnection);
        System.out.println("Oracle APP user is ready; run Flyway migrations as a separate task.");
    }

    static void bootstrap(Settings settings, Connector connector) {
        try (var admin = connector.connect(settings.url(), settings.adminUser(), settings.adminPassword())) {
            boolean appExists = exists(admin, "SELECT COUNT(*) FROM dba_users WHERE username = 'APP'");
            if (appExists) {
                // Fail before changing anything if the stored secret no longer
                // matches the existing account. Bootstrap never rotates passwords.
                verifyAppLogin(settings, connector);
            }
            try (var statement = admin.createStatement()) {
                if (!exists(admin, "SELECT COUNT(*) FROM dba_tablespaces WHERE tablespace_name = 'APP_DATA'")) {
                    // RDS manages the datafile location; no SYSDBA or local path.
                    statement.executeUpdate("CREATE TABLESPACE APP_DATA DATAFILE SIZE 20M "
                        + "AUTOEXTEND ON NEXT 10M MAXSIZE 100M");
                }
                if (!appExists) {
                    try (var createUser = admin.prepareCall("BEGIN EXECUTE IMMEDIATE "
                            + "'CREATE USER APP IDENTIFIED BY \"' || ? || "
                            + "'\" DEFAULT TABLESPACE APP_DATA QUOTA 100M ON APP_DATA'; END;")) {
                        createUser.setString(1, settings.appPassword());
                        createUser.execute();
                    }
                }
                statement.executeUpdate("ALTER USER APP DEFAULT TABLESPACE APP_DATA QUOTA 100M ON APP_DATA");
                statement.executeUpdate("GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE TO APP");
            }
            verifyAppLogin(settings, connector);
        } catch (SQLException error) {
            // JDBC exceptions can contain SQL text (including CREATE USER's
            // password). Preserve the Oracle error number, not the SQL or cause.
            throw new IllegalStateException("Oracle bootstrap failed (error " + error.getErrorCode()
                + "). Check connectivity, lab credentials and grants; no credentials were logged.");
        }
    }

    private static boolean exists(Connection connection, String sql) throws SQLException {
        try (var statement = connection.createStatement(); var result = statement.executeQuery(sql)) {
            result.next();
            return result.getInt(1) > 0;
        }
    }

    private static void verifyAppLogin(Settings settings, Connector connector) throws SQLException {
        try (var app = connector.connect(settings.url(), "APP", settings.appPassword());
             var statement = app.createStatement(); var result = statement.executeQuery("SELECT 1 FROM dual")) {
            if (!result.next() || result.getInt(1) != 1) {
                throw new SQLException("APP connection check failed");
            }
        }
    }
}
