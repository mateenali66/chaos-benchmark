################################################################################
# wrk2 Kubernetes Job Template
# Placeholders: ${JOB_NAME}, ${NAMESPACE}, ${ECR_REPO}, ${WRK_DURATION},
#               ${WRK_RATE}, ${WRK_THREADS}, ${WRK_CONNECTIONS}
#
# The NAMESPACE placeholder defaults to "social-network"
# (chaoslib.render_wrk2_job's default namespace), so an unmodified /
# CHAOS_SLOT-unset invocation renders byte-identical YAML to before slot
# parallelism existed. For slot parallelism it becomes
# "social-network-<slot>", targeting that slot's isolated copy of
# nginx-thrift. (Note: this whole header comment is itself substituted by
# the same naive string-replace as the manifest body -- pre-existing
# behavior, cosmetic only, harmless.)
################################################################################

apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
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
            - "http://nginx-thrift.${NAMESPACE}:8080"
          env:
            - name: TARGET_HOST
              value: "nginx-thrift.${NAMESPACE}"
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
