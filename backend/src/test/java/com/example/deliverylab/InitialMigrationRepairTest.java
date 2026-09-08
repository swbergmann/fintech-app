package com.example.deliverylab;

import java.util.List;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class InitialMigrationRepairTest {
    private final InitialMigrationRepair.History failed =
        new InitialMigrationRepair.History("1", "V1__create_customers.sql", false);

    @Test void acceptsOnlyTheFailedInitialMigrationOrEmptyHistory() {
        assertDoesNotThrow(() -> InitialMigrationRepair.validate("APP", false, List.of(failed)));
        assertDoesNotThrow(() -> InitialMigrationRepair.validate("APP", false, List.of()));
    }

    @Test void refusesExistingCustomersAndDifferentUsers() {
        assertThrows(IllegalStateException.class, () -> InitialMigrationRepair.validate("APP", true, List.of(failed)));
        assertThrows(IllegalStateException.class, () -> InitialMigrationRepair.validate("SYSTEM", false, List.of(failed)));
    }

    @Test void refusesSuccessfulOrUnrelatedMigrationHistory() {
        assertThrows(IllegalStateException.class, () -> InitialMigrationRepair.validate("APP", false,
            List.of(new InitialMigrationRepair.History("1", "V1__create_customers.sql", true))));
        assertThrows(IllegalStateException.class, () -> InitialMigrationRepair.validate("APP", false,
            List.of(failed, new InitialMigrationRepair.History("2", "V2__other.sql", false))));
        assertThrows(IllegalStateException.class, () -> InitialMigrationRepair.validate("APP", false,
            List.of(new InitialMigrationRepair.History("1", "V1__something_else.sql", false))));
    }
}
