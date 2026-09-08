package com.example.deliverylab;

import jakarta.validation.Valid;
import java.util.List;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

@RestController
@RequestMapping("/api/customers")
public class CustomerController {
    private final CustomerRepository repository;
    public CustomerController(CustomerRepository repository) { this.repository = repository; }

    @GetMapping public List<Customer> list() { return repository.findAll(); }

    @PostMapping @ResponseStatus(HttpStatus.CREATED)
    public Customer create(@Valid @RequestBody CustomerInput input) { return repository.create(input); }

    @DeleteMapping("/{id}") @ResponseStatus(HttpStatus.NO_CONTENT)
    public void delete(@PathVariable long id) {
        if (!repository.delete(id)) throw new ResponseStatusException(HttpStatus.NOT_FOUND);
    }
}
