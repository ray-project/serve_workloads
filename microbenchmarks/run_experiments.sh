#!/bin/bash
HOST=''
BEARER_TOKEN=''
for payload_size in 0 1000000 10000000
do
    for num_users in 500 1000 2000 3000 3500 4000 6000 8000
    do
        echo "Running with payload size $payload_size and $num_users users."
        python locust_runner.py -f qps_test_locustfile.py -u $num_users -r 100 -p $payload_size --host $HOST -b $BEARER_TOKEN -t 5m
    done
done
