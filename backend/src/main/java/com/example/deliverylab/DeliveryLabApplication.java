package com.example.deliverylab;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class DeliveryLabApplication {

	public static void main(String[] args) {
		if (java.util.Arrays.asList(args).contains("--bootstrap-rds")) {
			if (args.length != 1) {
				throw new IllegalArgumentException("Run --bootstrap-rds as a separate operation.");
			}
			RdsDatabaseBootstrap.run(System.getenv());
			return;
		}
		if (java.util.Arrays.asList(args).contains("--repair-initial")) {
			InitialMigrationRepair.run();
			return;
		}
		var context = SpringApplication.run(DeliveryLabApplication.class, args);
		if ("true".equals(System.getenv("MIGRATE_ONLY"))) {
			System.exit(SpringApplication.exit(context));
		}
	}

}
