## Ingest

- pass 1, batch 100: 157,452 events in 14.876 s = 10,584/s; valid: yes
- pass 1, batch 1000: 157,452 events in 12.185 s = 12,922/s; valid: yes
- pass 1, batch 5000: 157,452 events in 11.771 s = 13,376/s; valid: yes
- pass 2, batch 5000: 157,452 events in 11.804 s = 13,339/s; valid: yes
- pass 2, batch 1000: 157,452 events in 12.368 s = 12,731/s; valid: yes
- pass 2, batch 100: 157,452 events in 14.062 s = 11,197/s; valid: yes

## Queries (median of the repetitions)

| Query | Found | Index ms (pass 1, 2) | Scan ms (pass 1, 2) | Events read, index / scan | Valid |
|---|---|---|---|---|---|
| newest 100, no query | 100 | 0.69, 0.55 | 633, 609 | 101 / 157,452 | NO |
| a rare value: status:504 | 100 | 1.31, 1.33 | 3480, 3506 | 101 / 157,452 | NO |
| a common value: level:info | 100 | 2.83, 2.78 | 3484, 3510 | 101 / 157,452 | NO |
| one request: request_id | 1 | 0.25, 0.36 | 3391, 3481 | 1 / 157,452 | NO |
| a boolean: rate_limited:true | 100 | 1.52, 1.52 | 3493, 3466 | 101 / 157,452 | NO |
| field and range: route + status>=500 | 100 | 1.44, 1.48 | 3524, 3530 | 101 / 157,452 | NO |
| a range over durations: duration_ms>400 | 100 | 5.58, 5.32 | 3666, 3634 | 101 / 157,452 | NO |
| NOT: NOT level:info | 100 | 4.09, 12.37 | 3458, 3608 | 101 / 157,452 | NO |
| SurgiServe: event:upload.rejected | 82 | 1.00, 1.23 | 3414, 3526 | 82 / 157,452 | NO |
| SurgiServe: service:worker event:job.succeeded | 100 | 1.00, 1.08 | 3522, 3606 | 101 / 157,452 | NO |
| a phrase: "connection refused" | 100 | 2.70, 2.91 | 2815, 2812 | 101 / 157,452 | NO |
| UI timeline: histogram, no query | 157452 | 0.05, 0.05 | - | 0 | NO |
| UI fields: field list | 157452 | 0.14, 0.15 | - | 0 | NO |
| UI top values: route | 157452 | 0.10, 0.09 | - | 0 | NO |
