# ORCA evaluation report

Generated 2026-10-25 12:46 on 60 held-out chaos episodes (seeds 50000+, disjoint from training) and 6 fault-free runs in 95s.

Failure model: gradient-boosted trees - validation AUC **0.889** (logistic-regression baseline 0.782), 136486 training samples, horizon 15 ticks (1 tick = 5 s).

## Headline

* **92%** of service failures were predicted before they happened, on average **14.0 ticks (~70 s) early**; the first failure of each incident was anticipated **14.8 ticks** ahead.
* **81%** of risk flags were followed by a real failure (fault-free runs: 0.0% of ticks raised any flag).
* Root cause: top-1 service **100%**, top-3 **100%**, fault type **100%** - at the moment the first recommendation is made.
* Recovery (60-tick user availability on the real system): no action **69.8%** -> ORCA's first recommendation **92.4%** (textbook expert fix 91.9%).
* Closed loop (ORCA autopilot re-planning every 3 ticks, 80 ticks from fault injection): availability **72.3% -> 93.5%**.

## Per fault type

| Fault | Episodes | Failures predicted | Flag precision | Mean lead (ticks) | Incident lead | RCA top-1 | RCA top-3 | Fault type | Diagnosed after | Avail. no action | Avail. ORCA 1st rec. | Avail. expert | Autopilot: none -> ORCA |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Memory leak (bad release) | 10 | 94% | 47% | 16.6 | 16.0 | 100% | 100% | 100% | 31.6 ticks | 94% | 99% | 99% | 96% -> 100% |
| Traffic surge | 10 | 94% | 87% | 13.9 | 9.2 | 100% | 100% | 100% | 5.0 ticks | 42% | 62% | 59% | 40% -> 69% |
| Database lock contention | 10 | 87% | 86% | 19.5 | 20.2 | 100% | 100% | 100% | 3.9 ticks | 66% | 94% | 94% | 64% -> 95% |
| Cache memory exhaustion | 10 | 95% | 96% | 6.1 | 8.0 | 100% | 100% | 100% | 18.8 ticks | 55% | 100% | 100% | 61% -> 100% |
| Faulty deployment | 10 | 94% | 47% | 10.9 | 9.8 | 100% | 100% | 100% | 4.2 ticks | 82% | 99% | 99% | 82% -> 99% |
| Disk filling up | 10 | 87% | 73% | 16.9 | 25.8 | 100% | 100% | 100% | 32.5 ticks | 79% | 100% | 100% | 91% -> 100% |

## Recommendations made

| Fault | Target | Diagnosis | Recommended strategy | No action | ORCA |
|---|---|---|---|---|---|
| memory_leak | payment-service | memory_leak @ payment-service (99%) | Roll back release payment-service | 98.0% | 100.0% |
| memory_leak | payment-service | memory_leak @ payment-service (99%) | Roll back release payment-service | 98.0% | 100.0% |
| memory_leak | api-gateway | memory_leak @ api-gateway (99%) | Roll back release api-gateway | 90.5% | 100.0% |
| memory_leak | inventory-service | memory_leak @ inventory-service (99%) | Roll back release inventory-service | 94.7% | 100.0% |
| memory_leak | catalog-service | memory_leak @ catalog-service (99%) | Roll back release catalog-service | 62.8% | 93.7% |
| memory_leak | notification-service | memory_leak @ notification-service (99%) | No action (observe) | 100.0% | 100.0% |
| memory_leak | notification-service | memory_leak @ notification-service (94%) | No action (observe) | 100.0% | 100.0% |
| memory_leak | recommendation-service | memory_leak @ recommendation-service (99%) | Roll back release recommendation-service | 100.0% | 100.0% |
| memory_leak | recommendation-service | memory_leak @ recommendation-service (99%) | Scale out recommendation-service | 100.0% | 100.0% |
| memory_leak | payment-service | memory_leak @ payment-service (99%) | Roll back release payment-service | 98.0% | 100.0% |
| traffic_surge | api-gateway | traffic_surge @ api-gateway (99%) | Rate-limit ingress to 600 req/s + Scale out catalog-service + Scale out cart-service + Scale out order-service | 41.5% | 72.7% |
| traffic_surge | api-gateway | traffic_surge @ api-gateway (99%) | Scale out catalog-service | 42.3% | 43.5% |
| traffic_surge | api-gateway | traffic_surge @ api-gateway (99%) | Scale out catalog-service + Scale out auth-service + Scale out payment-service | 39.4% | 44.1% |
| traffic_surge | api-gateway | traffic_surge @ api-gateway (99%) | Rate-limit ingress to 600 req/s + Scale out catalog-service + Scale out recommendation-service + Scale out order-service | 38.2% | 69.8% |
| traffic_surge | api-gateway | traffic_surge @ api-gateway (99%) | Rate-limit ingress to 600 req/s + Scale out catalog-service + Scale out recommendation-service + Scale out cart-service | 38.6% | 69.3% |
| traffic_surge | api-gateway | traffic_surge @ api-gateway (99%) | Scale out catalog-service | 51.4% | 50.9% |
| traffic_surge | api-gateway | traffic_surge @ api-gateway (99%) | Rate-limit ingress to 600 req/s + Scale out catalog-service + Scale out order-service + Scale out cart-service | 44.2% | 74.9% |
| traffic_surge | api-gateway | traffic_surge @ api-gateway (99%) | Rate-limit ingress to 680 req/s + Scale out catalog-service + Scale out order-service + Scale out payment-service | 37.2% | 77.2% |
| traffic_surge | api-gateway | traffic_surge @ api-gateway (99%) | Rate-limit ingress to 600 req/s + Scale out catalog-service + Scale out auth-service + Scale out cart-service | 42.6% | 74.9% |
| traffic_surge | api-gateway | traffic_surge @ api-gateway (99%) | Scale out catalog-service | 41.4% | 42.8% |
| db_slow_queries | users-db | db_slow_queries @ users-db (80%) | Fail over database users-db | 52.1% | 92.1% |
| db_slow_queries | product-db | db_slow_queries @ product-db (80%) | Fail over database product-db | 60.0% | 91.3% |
| db_slow_queries | product-db | db_slow_queries @ product-db (80%) | Fail over database product-db | 59.5% | 91.3% |
| db_slow_queries | orders-db | db_slow_queries @ orders-db (80%) | Fail over database orders-db | 95.4% | 98.3% |
| db_slow_queries | product-db | db_slow_queries @ product-db (80%) | Fail over database product-db | 55.6% | 91.3% |
| db_slow_queries | users-db | db_slow_queries @ users-db (80%) | Fail over database users-db | 51.1% | 92.1% |
| db_slow_queries | product-db | db_slow_queries @ product-db (81%) | Fail over database product-db | 52.3% | 91.2% |
| db_slow_queries | orders-db | db_slow_queries @ orders-db (80%) | Fail over database orders-db | 95.4% | 98.3% |
| db_slow_queries | users-db | db_slow_queries @ users-db (79%) | Fail over database users-db | 47.1% | 92.2% |
| db_slow_queries | orders-db | db_slow_queries @ orders-db (81%) | Fail over database orders-db | 95.0% | 98.3% |
| cache_crash | redis-cache | cache_crash @ redis-cache (99%) | Flush oversized keys redis-cache | 59.5% | 100.0% |
| cache_crash | redis-cache | cache_crash @ redis-cache (99%) | Flush oversized keys redis-cache | 55.8% | 100.0% |
| cache_crash | redis-cache | cache_crash @ redis-cache (99%) | Flush oversized keys redis-cache | 50.7% | 100.0% |
| cache_crash | redis-cache | cache_crash @ redis-cache (99%) | Flush oversized keys redis-cache | 57.5% | 100.0% |
| cache_crash | redis-cache | cache_crash @ redis-cache (99%) | Flush oversized keys redis-cache | 57.5% | 100.0% |
| cache_crash | redis-cache | cache_crash @ redis-cache (99%) | Flush oversized keys redis-cache | 47.9% | 100.0% |
| cache_crash | redis-cache | cache_crash @ redis-cache (99%) | Flush oversized keys redis-cache | 48.5% | 100.0% |
| cache_crash | redis-cache | cache_crash @ redis-cache (99%) | Flush oversized keys redis-cache | 57.5% | 100.0% |
| cache_crash | redis-cache | cache_crash @ redis-cache (99%) | Flush oversized keys redis-cache | 59.6% | 100.0% |
| cache_crash | redis-cache | cache_crash @ redis-cache (99%) | Flush oversized keys redis-cache | 57.5% | 100.0% |
| bad_deploy | inventory-service | bad_deploy @ inventory-service (98%) | Roll back release inventory-service | 71.2% | 99.8% |
| bad_deploy | catalog-service | bad_deploy @ catalog-service (93%) | Roll back release catalog-service | 60.1% | 96.2% |
| bad_deploy | api-gateway | bad_deploy @ api-gateway (94%) | Roll back release api-gateway | 70.7% | 99.5% |
| bad_deploy | search-service | bad_deploy @ search-service (98%) | Roll back release search-service | 95.2% | 99.9% |
| bad_deploy | recommendation-service | bad_deploy @ recommendation-service (99%) | No action (observe) | 100.0% | 100.0% |
| bad_deploy | auth-service | bad_deploy @ auth-service (98%) | Roll back release auth-service | 69.9% | 99.1% |
| bad_deploy | cart-service | bad_deploy @ cart-service (99%) | Roll back release cart-service | 90.6% | 99.6% |
| bad_deploy | recommendation-service | bad_deploy @ recommendation-service (99%) | No action (observe) | 100.0% | 100.0% |
| bad_deploy | payment-service | bad_deploy @ payment-service (90%) | Roll back release payment-service | 93.6% | 99.9% |
| bad_deploy | inventory-service | bad_deploy @ inventory-service (98%) | Roll back release inventory-service | 68.4% | 99.8% |
| disk_fill | orders-db | disk_fill @ orders-db (99%) | Purge & expand storage orders-db | 97.1% | 100.0% |
| disk_fill | orders-db | disk_fill @ orders-db (93%) | Purge & expand storage orders-db | 98.2% | 100.0% |
| disk_fill | users-db | disk_fill @ users-db (95%) | Purge & expand storage users-db | 52.9% | 100.0% |
| disk_fill | product-db | disk_fill @ product-db (99%) | Purge & expand storage product-db | 49.8% | 100.0% |
| disk_fill | kafka | disk_fill @ kafka (94%) | Purge & expand storage kafka | 95.8% | 100.0% |
| disk_fill | elasticsearch | disk_fill @ elasticsearch (99%) | Purge & expand storage elasticsearch | 86.3% | 100.0% |
| disk_fill | product-db | disk_fill @ product-db (99%) | Purge & expand storage product-db | 73.8% | 100.0% |
| disk_fill | orders-db | disk_fill @ orders-db (99%) | Purge & expand storage orders-db | 99.2% | 100.0% |
| disk_fill | elasticsearch | disk_fill @ elasticsearch (99%) | Purge & expand storage elasticsearch | 86.6% | 100.0% |
| disk_fill | users-db | disk_fill @ users-db (99%) | Purge & expand storage users-db | 53.0% | 100.0% |
