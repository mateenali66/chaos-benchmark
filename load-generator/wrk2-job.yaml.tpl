################################################################################
# wrk2 Kubernetes Job Template
# Placeholders: ${JOB_NAME}, ${ECR_REPO}, ${WRK_DURATION}, ${WRK_RATE},
#               ${WRK_THREADS}, ${WRK_CONNECTIONS}
################################################################################

apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: social-network
  labels:
    app.kubernetes.io/part-of: chaos-benchmark
    app.kubernetes.io/component: load-generator
spec:
  ttlSecondsAfterFinished: 300
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: wrk2-load-generator
        job-name: ${JOB_NAME}
    spec:
      restartPolicy: Never
      containers:
        - name: wrk2
          image: ${ECR_REPO}:latest
          args:
            - "-t${WRK_THREADS}"
            - "-c${WRK_CONNECTIONS}"
            - "-d${WRK_DURATION}s"
            - "-R${WRK_RATE}"
            - "-L"
            - "-s"
            - "/scripts/mixed-workload.lua"
            - "http://nginx-thrift.social-network:8080"
          env:
            - name: TARGET_HOST
              value: "nginx-thrift.social-network"
            - name: TARGET_PORT
              value: "8080"
            - name: MAX_USER_INDEX
              value: "962"
          resources:
            requests:
              cpu: 500m
              memory: 256Mi
            limits:
              cpu: "2"
              memory: 512Mi
