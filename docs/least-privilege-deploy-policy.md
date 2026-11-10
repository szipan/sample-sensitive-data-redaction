# 部署最小权限说明

本目录有两份 IAM 策略，用途不同：

| 文件 | 附加给谁 | 作用 |
| --- | --- | --- |
| `least-privilege-deploy-policy.json` | 执行 `cdk bootstrap` / `cdk deploy` 的**部署身份**(IAM 用户或角色) | 只允许扮演 CDK bootstrap 角色、读取本应用的栈,以及一次性的 bootstrap |
| `least-privilege-cfn-exec-policy.json` | CDK 的 **CloudFormation 执行角色**(可选加固) | 替换 bootstrap 默认的 `AdministratorAccess`,只允许创建、更新、删除本应用的资源 |

两份都不是运行时权限。栈里每个 Lambda 和 AgentCore Runtime 的执行角色各有按资源收敛的策略,与这两份文件无关。

## 依据

策略来自本账号实际部署的 CloudTrail 记录(2026-11-03 多次 `cdk deploy`,包括新建 Cognito、授权器、用户管理接口的更新),并参照 `kiro-dashboard-on-agentcore/docs/least-privilege-deploy-policy.json` 的格式。

CloudTrail 显示,用现代 CDK bootstrap 部署时:

- **部署身份本人**只调用了 `sts:AssumeRole`(扮演 `cdk-hnb659fds-deploy-role`、`file-publishing-role`、`lookup-role`)和几个只读查询。
- **资源全部由 CloudFormation 执行角色创建**(`cdk-hnb659fds-cfn-exec-role`):Lambda、S3、DynamoDB、SQS、API Gateway、Cognito、Bedrock Guardrail、AgentCore Runtime、IAM 角色。
- 本应用没有 Docker 镜像资产,所以不需要 `image-publishing-role` 和 ECR 推送权限。

两份策略都通过了 IAM Access Analyzer `validate-policy`(0 个问题),压缩后分别约 2.3KB 和 4.6KB,在托管策略 6144 字符上限内。另用 IAM 策略模拟器核对了 66 个用例:实际用到的调用(含更新和删除)全部放行;越权调用全部拒绝,比如部署身份直接创建 Lambda、操作其他应用的栈、挂 `AdministratorAccess`、把角色传给 EC2。

## 部署身份策略(`least-privilege-deploy-policy.json`)

| Sid | 用途 |
| --- | --- |
| `CdkAssumeDeployRoles` | 扮演 CDK 的 deploy / file-publishing / lookup 角色,`cdk deploy` / `cdk diff` 只需要这一条 |
| `AppStackRead` | 读取本应用栈的状态和输出(README 第 5 步取 API 地址和用户池 ID) |
| `ApiEndpointLookup` | `aws apigatewayv2 get-apis`,只能列 API,不能修改 |
| `AdminRecoveryOnUserPool` | README 里"所有管理员都无法登录"时,用 CLI 把某人加回 `admins` 组。不需要可删除 |
| `Bootstrap*` | **仅首次 `cdk bootstrap` 需要**:`CDKToolkit` 栈、`cdk-hnb659fds-*` 桶/ECR 仓库/角色、SSM 版本参数,以及可选的执行角色策略 `PiiRedactionCdkExecution`。账号已 bootstrap 后可以删掉这些语句 |
| `AccountWideUnscopable` | `sts:GetCallerIdentity`、`cloudformation:ValidateTemplate` / `GetTemplateSummary`:不支持资源级限定 |

部署身份本身**没有**任何 Lambda、S3 数据、DynamoDB、IAM 写权限,也不能 `iam:PassRole`(由 deploy 角色把执行角色交给 CloudFormation)。

## 重要:默认 bootstrap 下的实际权限边界

默认 `cdk bootstrap` 给 CloudFormation 执行角色挂的是 **`AdministratorAccess`**,而且这个角色由同一账号、同一区域的所有 CDK 应用共用(本账号的 `KiroDash-*` 等栈也在用)。所以:

- 部署身份策略本身很小,但**能扮演 deploy 角色的人可以通过 CloudFormation 部署任意资源**。
- 不能直接把共用执行角色换成本应用的收敛策略,否则其他 CDK 应用会部署失败。

真正的最小权限要给本应用**单独做一次 bootstrap**(独立 qualifier),执行角色挂 `least-privilege-cfn-exec-policy.json`:

```bash
Q=piired                                     # 自选,最多 10 个小写字母或数字
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# 1. 把两份策略里的 hnb659fds 换成你的 qualifier
sed -i "s/hnb659fds/$Q/g" docs/least-privilege-*.json
# 部署身份策略里的 CDKToolkit 也要换成下一步的工具栈名
sed -i "s#stack/CDKToolkit/#stack/CDKToolkit-PiiRedaction/#" docs/least-privilege-deploy-policy.json

# 2. 创建执行角色策略,再用它做 bootstrap
aws iam create-policy --policy-name PiiRedactionCdkExecution \
  --policy-document file://docs/least-privilege-cfn-exec-policy.json
cdk bootstrap aws://$ACCOUNT/us-west-2 \
  --qualifier $Q --toolkit-stack-name CDKToolkit-PiiRedaction \
  --cloudformation-execution-policies arn:aws:iam::$ACCOUNT:policy/PiiRedactionCdkExecution

# 3. 让应用使用这个 qualifier:在 backend/cdk.json 的 "context" 里加入
#    "@aws-cdk/core:bootstrapQualifier": "piired"
```

之后部署身份只需附加改好的 `least-privilege-deploy-policy.json`。

## 执行角色策略(`least-privilege-cfn-exec-policy.json`)

资源按本应用的命名收敛:

- **S3**:桶 `piidocumentredactionpocst*`(栈名截断后的前缀);只读 CDK 资产桶,因为创建 Lambda 和 AgentCore Runtime 时要读代码包。
- **Lambda**:函数 `PiiDocumentRedactionPocSt*`、事件源映射。
- **DynamoDB**:表 `RedactionJobs`(含 stream)、`RedactionRules`、`RuleOperations`。
- **SQS**:队列 `RuleOperationsQueue.fifo`。
- **IAM**:角色 `PiiDocumentRedactionPocSt*`。
  - `AttachRolePolicy` 只允许挂 `AWSLambdaBasicExecutionRole`。
  - `PassRole` 只能传给 `lambda.amazonaws.com` 和 `bedrock-agentcore.amazonaws.com`。
  - 只允许为 `bedrock-agentcore` 创建服务关联角色,全新账号首次创建 Runtime 时需要。
- **AgentCore**:Runtime `pii_redaction_agent-*` 及其 workload identity。
- **KMS**:只能经由 S3、Lambda、SQS、DynamoDB 使用(`kms:ViaService` 条件),对应各服务的 AWS 托管密钥。

以下三组保留了服务级通配的动作,资源已限定:

| Sid | 动作 | 资源 | 原因 |
| --- | --- | --- | --- |
| `AppBuckets` | `s3:*` | 本应用两个桶 | CloudFormation 读写桶配置的动作很多(CloudTrail 里约 30 个),删除栈时还要清空和删除桶 |
| `AppFunctions` | `lambda:*` | 本应用函数 | 创建、更新、删除、调用自定义资源,版本间动作会变化 |
| `HttpApi` / `UserPool` | `apigateway:*` / `cognito-idp:*` | `/apis*`、`userpool/*` | API Gateway 按 HTTP 方法授权;Cognito 用户池 ID 是随机的,无法按名称限定 |

下列动作不支持资源级限定,只能用 `"Resource": "*"`:`bedrock:CreateGuardrail`、`bedrock-agentcore:CreateAgentRuntime`、`cognito-idp:CreateUserPool`、`lambda:CreateEventSourceMapping`、`lambda:GetAccountSettings`、`iam:GetAccountSummary`。

`AgentRuntimeCreateTimeChecks` 是实测补上的:`CreateAgentRuntime` 会同时创建 DEFAULT 端点并打标签,这时 Runtime ID 还没生成,AgentCore 用字面量 `runtime/*` 检查 `CreateAgentRuntimeEndpoint` 和 `TagResource`,按名称限定的 `runtime/pii_redaction_agent-*` 匹配不上,只能放开到 `runtime/*`。删除 Runtime 时还要 `DeleteAgentRuntimeEndpoint`,它能按名称限定,已放在 `AgentRuntime` 语句里。这几项 CloudTrail 和错误信息里都看不到(CloudFormation 只报 `Access denied for operation 'AWS::BedrockAgentCore::Runtime'`),是用同一策略直接调用 API 才查出来的。

## 实测结果(2026-11-10,us-west-2)

- 新建角色 `PiiRedactionDeployer`,只挂 `least-privilege-deploy-policy.json`(qualifier 换成 `piired`),用它完成了全部步骤:
  - `cdk bootstrap --qualifier piired --toolkit-stack-name CDKToolkit-PiiRedaction`,包括创建 `PiiRedactionCdkExecution` 策略;
  - 首次 `cdk deploy` 因缺少上面的 AgentCore 端点权限失败,CloudFormation 用收紧后的执行角色**完整回滚**,删除了已创建的约 100 个资源;
  - 补上权限后重新部署,CDK 先删除 `ROLLBACK_COMPLETE` 的栈再创建,119 个资源全部成功。
- 部署后验证:首个管理员收到邀请(`FORCE_CHANGE_PASSWORD`、在 `admins` 组);无 token 401,普通用户访问 `/rules`、`/users` 403,管理员 200。

## 未验证的部分

- **更新和 `cdk destroy`**:回滚覆盖了大部分资源的删除动作,但没有做过“有变更的更新”和正常的 `cdk destroy`(包括清空桶的自定义资源)。生产环境建议各做一次,再用 [IAM Access Analyzer 策略生成](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-policy-generation.html) 根据 CloudTrail 收紧通配语句。
- **Cognito 的调用**:CloudTrail `lookup-events` 没有返回 Cognito 的事件,所以 `UserPool` 语句用了服务级通配。
- **账号和区域**:策略里的账号和区域是 `*`。固定只部署到 `us-west-2` 的话,可以把 ARN 里的 `*:*` 换成 `us-west-2:<账号ID>`,进一步收紧。

## 参考

- [AWS CDK bootstrapping](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html)
- [Customize AWS CDK bootstrapping](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping-customizing.html)
- [Create and apply permissions boundaries for the AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/customize-permissions-boundaries.html)
