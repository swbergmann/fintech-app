package com.example.deliverylab;

import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import org.flywaydb.core.Flyway;

/** Operator-requested recovery of the single failed initial migration, never routine deployment. */
public final class InitialMigrationRepair {
    private InitialMigrationRepair() {}

    record History(String version, String script, boolean success) {}

    static void validate(String user, boolean customersExist, List<History> history) {
        if (!"APP".equalsIgnoreCase(user) || customersExist) {
            throw new IllegalStateException("Refusing repair: expected APP with no CUSTOMERS table.");
        }
        if (history.isEmpty()) return;
        if (history.size() != 1 || history.getFirst().success()
                || !"1".equals(history.getFirst().version())
                || !"V1__create_customers.sql".equals(history.getFirst().script())) {
            throw new IllegalStateException("Refusing repair: history is not limited to the failed initial V1 migration.");
        }
    }

    public static void run() {
        var flyway = Flyway.configure()
            .dataSource(System.getenv("DB_URL"), System.getenv("DB_USERNAME"), System.getenv("DB_PASSWORD"))
            .locations("classpath:db/migration")
            .cleanDisabled(true)
            .load();
        var history = new ArrayList<History>();
        try (var connection = flyway.getConfiguration().getDataSource().getConnection();
             var statement = connection.createStatement()) {
            boolean customersExist;
            try (var result = statement.executeQuery(
                    "SELECT COUNT(*) FROM user_tables WHERE UPPER(table_name) = 'CUSTOMERS'")) {
                result.next(); customersExist = result.getInt(1) > 0;
            }
            boolean historyExists;
            try (var result = statement.executeQuery(
                    "SELECT COUNT(*) FROM user_tables WHERE table_name = 'flyway_schema_history'")) {
                result.next(); historyExists = result.getInt(1) > 0;
            }
            if (historyExists) {
                try (var result = statement.executeQuery(
                        "SELECT \"version\", \"script\", \"success\" FROM \"flyway_schema_history\"")) {
                    while (result.next()) {
                        history.add(new History(result.getString(1), result.getString(2), result.getInt(3) == 1));
                    }
                }
            }
            validate(connection.getMetaData().getUserName(), customersExist, history);
        } catch (SQLException error) {
            throw new IllegalStateException("Cannot inspect initial migration state; repair was not run.", error);
        }
        if (history.isEmpty()) {
            System.out.println("No failed initial migration record to repair.");
            return;
        }
        flyway.repair();
        System.out.println("Removed the failed initial V1 history entry. No application tables were dropped.");
    }
}
