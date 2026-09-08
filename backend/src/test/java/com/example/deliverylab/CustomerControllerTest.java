package com.example.deliverylab;

import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class CustomerControllerTest {
    private static class MemoryRepository implements CustomerRepository {
        private final List<Customer> records = new ArrayList<>();
        public List<Customer> findAll() { return List.copyOf(records); }
        public Customer create(CustomerInput input) {
            var customer = new Customer(1, input.name(), input.email());
            records.add(customer); return customer;
        }
        public boolean delete(long id) { return records.removeIf(record -> record.id() == id); }
    }

    @Test void createsListsAndDeletesCustomers() throws Exception {
        var mvc = MockMvcBuilders.standaloneSetup(new CustomerController(new MemoryRepository())).build();
        mvc.perform(post("/api/customers").contentType(MediaType.APPLICATION_JSON)
            .content("{\"name\":\"Alex Example\",\"email\":\"alex@example.com\"}"))
            .andExpect(status().isCreated()).andExpect(jsonPath("$.id").value(1));
        mvc.perform(get("/api/customers")).andExpect(jsonPath("$[0].name").value("Alex Example"));
        mvc.perform(delete("/api/customers/1")).andExpect(status().isNoContent());
        mvc.perform(delete("/api/customers/1")).andExpect(status().isNotFound());
    }

    @Test void rejectsInvalidInput() throws Exception {
        var mvc = MockMvcBuilders.standaloneSetup(new CustomerController(new MemoryRepository())).build();
        mvc.perform(post("/api/customers").contentType(MediaType.APPLICATION_JSON)
            .content("{\"name\":\" \",\"email\":\"invalid\"}"))
            .andExpect(status().isBadRequest());
        mvc.perform(get("/api/customers")).andExpect(content().json("[]"));
    }
}
