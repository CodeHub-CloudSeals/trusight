#!/usr/bin/env python3
"""AWS CDK app for the TrustSight demonstrator.

Architecture rationale
----------------------
Steps 12 and 19 suspend the run pending human action, potentially for days.
Step Functions' ``waitForTaskToken`` integration is the natural primitive for
that: the state machine parks, holds no compute, survives deployments, and
resumes when SendTaskSuccess arrives with the estimator's answer. A polling
loop or an in-memory agent framework cannot do this, which is why the choice
is made here rather than left to the application.

  S3            drawings (versioned, immutable), rendered pages, exports
  Step Functions  the 23-step workflow; task tokens for approval gates
  Lambda        deterministic and LLM steps (short, idempotent)
  Fargate       PDF-heavy extraction (memory and time beyond Lambda limits)
  Bedrock       Claude models for the five generative agents
  DynamoDB      knowledge graph, evidence chain, run state
  API Gateway   the FastAPI surface behind a Lambda adapter
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_dynamodb as ddb,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct


class TrustSightStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kw) -> None:
        super().__init__(scope, cid, **kw)

        # ---------------------------------------------------------------- storage
        drawings = s3.Bucket(
            self, "Drawings",
            versioned=True,                       # originals are immutable
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        artifacts = s3.Bucket(
            self, "Artifacts",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Evidence records are append-only: the IAM policy below grants
        # PutItem but never UpdateItem or DeleteItem (spec s10).
        evidence = ddb.Table(
            self, "Evidence",
            partition_key=ddb.Attribute(name="run_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="record_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        graph = ddb.Table(
            self, "KnowledgeGraph",
            partition_key=ddb.Attribute(name="project_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="element_key", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        runs = ddb.Table(
            self, "Runs",
            partition_key=ddb.Attribute(name="run_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        common_env = {
            "DRAWINGS_BUCKET": drawings.bucket_name,
            "ARTIFACTS_BUCKET": artifacts.bucket_name,
            "EVIDENCE_TABLE": evidence.table_name,
            "GRAPH_TABLE": graph.table_name,
            "RUNS_TABLE": runs.table_name,
            "TRUSTSIGHT_MODEL": "anthropic.claude-sonnet-4-5-20250929-v1:0",
        }

        bedrock_policy = iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=["arn:aws:bedrock:*::foundation-model/anthropic.*"],
        )

        def fn(name: str, handler: str, *, timeout=60, memory=1024) -> lambda_.Function:
            f = lambda_.Function(
                self, name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=handler,
                code=lambda_.Code.from_asset("../", exclude=["infra", ".git", "tests"]),
                timeout=Duration.seconds(timeout),
                memory_size=memory,
                environment=common_env,
                log_retention=logs.RetentionDays.ONE_MONTH,
            )
            drawings.grant_read(f)
            artifacts.grant_read_write(f)
            graph.grant_read_write_data(f)
            runs.grant_read_write_data(f)
            # append-only: PutItem and reads, never Update or Delete
            evidence.grant(f, "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query")
            f.add_to_role_policy(bedrock_policy)
            return f

        preflight = fn("Preflight", "handlers.preflight.handler", timeout=120)
        classify = fn("Classify", "handlers.classify.handler", timeout=120)
        interpret = fn("Interpret", "handlers.interpret.handler", timeout=300, memory=2048)
        resolve_sheets = fn("ResolveSheets", "handlers.resolve.handler", timeout=180)
        calculate = fn("Calculate", "handlers.calculate.handler", timeout=120)
        gates = fn("Gates", "handlers.gates.handler")
        qa = fn("QA", "handlers.qa.handler", timeout=180)
        emit = fn("EmitBBS", "handlers.emit.handler", timeout=120)
        notify = fn("NotifyReviewer", "handlers.notify.handler")

        # Heavy PDF work: vector analysis and 300dpi rendering exceed Lambda
        # comfortably on large sheet sets, so it runs on Fargate.
        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=1)
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)
        task_def = ecs.FargateTaskDefinition(self, "ExtractTask", cpu=2048, memory_limit_mib=8192)
        container = task_def.add_container(
            "extract",
            image=ecs.ContainerImage.from_asset("../", file="Dockerfile"),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="extract"),
            environment=common_env,
        )
        drawings.grant_read(task_def.task_role)
        artifacts.grant_read_write(task_def.task_role)
        task_def.task_role.add_to_principal_policy(bedrock_policy)

        # ---------------------------------------------------------------- workflow
        def step(label: str, function: lambda_.Function) -> tasks.LambdaInvoke:
            return tasks.LambdaInvoke(
                self, label,
                lambda_function=function,
                payload_response_only=True,
                retry_on_service_exceptions=True,
                result_path=f"$.{label.lower()}",
            )

        extract_task = tasks.EcsRunTask(
            self, "Extract",
            cluster=cluster,
            task_definition=task_def,
            launch_target=tasks.EcsFargateLaunchTarget(),
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            container_overrides=[
                tasks.ContainerOverride(
                    container_definition=container,
                    environment=[
                        tasks.TaskEnvironmentVariable(
                            name="RUN_ID", value=sfn.JsonPath.string_at("$.run_id")
                        )
                    ],
                )
            ],
            result_path="$.extract",
        )

        # Steps 12 and 19: the run parks here holding no compute. There is no
        # timeout-to-approve path — a timeout fails the branch, it never
        # approves by default.
        human_gate = tasks.LambdaInvoke(
            self, "HumanApproval",
            lambda_function=notify,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object(
                {
                    "task_token": sfn.JsonPath.task_token,
                    "run_id": sfn.JsonPath.string_at("$.run_id"),
                    "questions": sfn.JsonPath.object_at("$.gates.questions"),
                }
            ),
            timeout=Duration.days(14),
            result_path="$.approval",
        )

        needs_human = sfn.Choice(self, "NeedsHuman?")
        definition = (
            step("Preflight", preflight)
            .next(step("Classify", classify))
            .next(extract_task)
            .next(step("Resolve", resolve_sheets))
            .next(step("Interpret", interpret))
            .next(step("Gates", gates))
            .next(
                needs_human.when(
                    sfn.Condition.boolean_equals("$.gates.requires_human", True),
                    human_gate.next(step("Calculate", calculate)),
                ).otherwise(step("CalculateDirect", calculate))
                .afterwards()
                .next(step("QA", qa))
                .next(step("EmitBBS", emit))
            )
        )

        machine = sfn.StateMachine(
            self, "Pipeline",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.days(30),
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=logs.LogGroup(
                    self, "PipelineLogs", retention=logs.RetentionDays.ONE_MONTH
                ),
                level=sfn.LogLevel.ALL,
            ),
        )

        # ---------------------------------------------------------------- api
        api_fn = lambda_.Function(
            self, "Api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handlers.api.handler",       # Mangum adapter over FastAPI
            code=lambda_.Code.from_asset("../", exclude=["infra", ".git", "tests"]),
            timeout=Duration.seconds(30),
            memory_size=1024,
            environment={**common_env, "STATE_MACHINE_ARN": machine.state_machine_arn},
            log_retention=logs.RetentionDays.ONE_MONTH,
        )
        machine.grant_start_execution(api_fn)
        machine.grant_task_response(api_fn)        # SendTaskSuccess on approval
        drawings.grant_read_write(api_fn)
        graph.grant_read_write_data(api_fn)
        runs.grant_read_write_data(api_fn)
        evidence.grant(api_fn, "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query")

        http = apigw.HttpApi(self, "HttpApi")
        http.add_routes(
            path="/{proxy+}",
            methods=[apigw.HttpMethod.ANY],
            integration=apigw.HttpLambdaIntegration("ApiIntegration", api_fn)
            if hasattr(apigw, "HttpLambdaIntegration")
            else None,
        )

        cdk.CfnOutput(self, "ApiUrl", value=http.url or "")
        cdk.CfnOutput(self, "StateMachineArn", value=machine.state_machine_arn)
        cdk.CfnOutput(self, "DrawingsBucket", value=drawings.bucket_name)


app = cdk.App()
TrustSightStack(
    app, "TrustSightDemo",
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region") or "eu-west-2",
    ),
)
app.synth()
