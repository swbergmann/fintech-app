package com.example.deliverylab;

import java.util.List;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.stereotype.Repository;

@Repository
public class JdbcCustomerRepository implements CustomerRepository {
    private final JdbcTemplate jdbc;

    public JdbcCustomerRepository(JdbcTemplate jdbc) { this.jdbc = jdbc; }

    public List<Customer> findAll() {
        return jdbc.query("SELECT id, name, email FROM customers ORDER BY id",
            (row, index) -> new Customer(row.getLong("id"), row.getString("name"), row.getString("email")));
    }

    public Customer create(CustomerInput input) {
        var keys = new GeneratedKeyHolder();
        jdbc.update(connection -> {
            var statement = connection.prepareStatement(
                "INSERT INTO customers (name, email) VALUES (?, ?)", new String[]{"ID"});
            statement.setString(1, input.name().trim());
            statement.setString(2, input.email().trim());
            return statement;
        }, keys);
        var key = keys.getKey();
        if (key == null) throw new IllegalStateException("Database did not return the customer ID");
        return new Customer(key.longValue(), input.name().trim(), input.email().trim());
    }

    public boolean delete(long id) {
        return jdbc.update("DELETE FROM customers WHERE id = ?", id) > 0;
    }
}
