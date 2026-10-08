## Ingest

- pass 1, batch 100: 157,452 events in 14.571 s = 10,806/s; valid: NO
- pass 1, batch 1000: 157,452 events in 12.382 s = 12,716/s; valid: yes
- pass 1, batch 5000: 157,452 events in 12.088 s = 13,025/s; valid: yes
- pass 2, batch 5000: 157,452 events in 11.602 s = 13,571/s; valid: yes
- pass 2, batch 1000: 157,452 events in 12.261 s = 12,842/s; valid: yes
- pass 2, batch 100: 157,452 events in 14.395 s = 10,938/s; valid: yes

## Queries (median of the repetitions)

| Query | Found | Index ms (pass 1, 2) | Scan ms (pass 1, 2) | Events read, index / scan | Valid |
|---|---|---|---|---|---|
| newest 100, no query | 100 | 0.59, 0.56 | 622, 615 | 101 / 157,452 | yes |
| a rare value: status:504 | 100 | 1.29, 1.52 | 3428, 3514 | 101 / 157,452 | yes |
| a common value: level:info | 100 | 3.01, 4.53 | 3435, 3704 | 101 / 157,452 | yes |
| one request: request_id | 1 | 0.26, 0.31 | 3362, 3513 | 1 / 157,452 | yes |
| a boolean: rate_limited:true | 100 | 1.46, 1.52 | 3408, 3438 | 101 / 157,452 | yes |
| field and range: route + status>=500 | 100 | 1.64, 1.35 | 3472, 3501 | 101 / 157,452 | NO |
| a range over durations: duration_ms>400 | 100 | 6.07, 6.01 | 3618, 3704 | 101 / 157,452 | yes |
| NOT: NOT level:info | 100 | 4.84, 12.45 | 3483, 3682 | 101 / 157,452 | NO |
| SurgiServe: event:upload.rejected | 82 | 0.99, 1.82 | 3353, 3774 | 82 / 157,452 | NO |
| SurgiServe: service:worker event:job.succeeded | 100 | 1.14, 1.78 | 3557, 3907 | 101 / 157,452 | NO |
| a phrase: "connection refused" | 100 | 2.81, 2.85 | 2772, 2766 | 101 / 157,452 | yes |
| UI timeline: histogram, no query | 157452 | 0.06, 0.05 | - | 0 | yes |
| UI fields: field list | 157452 | 0.13, 0.12 | - | 0 | yes |
| UI top values: route | 157452 | 0.10, 0.10 | - | 0 | yes |
