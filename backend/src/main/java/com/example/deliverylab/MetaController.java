package com.example.deliverylab;

import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class MetaController {
    private final String environment;
    private final String release;
    public MetaController(@Value("${lab.environment}") String environment, @Value("${lab.release}") String release) {
        this.environment = environment;
        this.release = release;
    }
    @GetMapping("/api/meta") public Map<String, String> meta() {
        return Map.of("environment", environment, "release", release);
    }
}
