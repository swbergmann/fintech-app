package com.example.deliverylab;

import java.util.List;

public interface CustomerRepository {
    List<Customer> findAll();
    Customer create(CustomerInput input);
    boolean delete(long id);
}
