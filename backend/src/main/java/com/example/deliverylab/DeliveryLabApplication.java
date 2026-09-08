package com.example.deliverylab;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class DeliveryLabApplication {

	public static void main(String[] args) {
		var context = SpringApplication.run(DeliveryLabApplication.class, args);
		if ("true".equals(System.getenv("MIGRATE_ONLY"))) {
			System.exit(SpringApplication.exit(context));
		}
	}

}
