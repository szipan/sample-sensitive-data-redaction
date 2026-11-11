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

两份策略都通过了 IAM Access Analyzer `validate-policy`(0 个问题),压缩后分别约 2.3KB 和 6.1KB,在托管策略 6144 字符上限内。另用 IAM 策略模拟器核对了 66 个用例:实际用到的调用(含更新和删除)全部放行;越权调用全部拒绝,比如部署身份直接创建 Lambda、操作其他应用的栈、挂 `AdministratorAccess`、把角色传给 EC2。

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
- **CloudWatch Logs**:`ApiLogGroup` 只允许创建、删除和配置 API 访问日志组 `/aws/apigateway/PiiDocumentRedactionPocApi-access`;`ApiLogDelivery` 是 API Gateway 开启访问日志时要求的日志投递权限(`*LogDelivery`、`PutResourcePolicy` 等),这些动作不支持资源级限定,只能用 `"Resource": "*"`。

以下三组保留了服务级通配的动作,资源已限定:

| Sid | 动作 | 资源 | 原因 |
| --- | --- | --- | --- |
| `AppBuckets` | `s3:*` | 本应用两个桶 | CloudFormation 读写桶配置的动作很多(CloudTrail 里约 30 个),删除栈时还要清空和删除桶 |
| `AppFunctions` | `lambda:*` | 本应用函数 | 创建、更新、删除、调用自定义资源,版本间动作会变化 |
| `HttpApi` / `UserPool` | `apigateway:*` / 明确的 17 个 `cognito-idp:*` 动作 | `/apis*`、`userpool/*` | API Gateway 按 HTTP 方法授权;Cognito 用户池 ID 是随机的,无法按名称限定,但动作已逐个列出(用户池、客户端、组的增删改查、标签),不含任何用户数据操作 |

下列动作不支持资源级限定,只能用 `"Resource": "*"`:`bedrock:CreateGuardrail`、`bedrock-agentcore:CreateAgentRuntime`、`cognito-idp:CreateUserPool`、`lambda:GetAccountSettings`、`iam:GetAccountSummary`,以及 Lambda 事件源映射的 `Create/Get/Update/Delete/ListEventSourceMapping`。后一组是实测补上的:映射的授权资源是 `*` 而不是映射 ARN,所以按 `event-source-mapping:*` 限定会被拒绝。

`AgentRuntimeCreateTimeChecks` 是实测补上的:`CreateAgentRuntime` 会同时创建 DEFAULT 端点并打标签,这时 Runtime ID 还没生成,AgentCore 用字面量 `runtime/*` 检查 `CreateAgentRuntimeEndpoint` 和 `TagResource`,按名称限定的 `runtime/pii_redaction_agent-*` 匹配不上,只能放开到 `runtime/*`。删除 Runtime 时还要 `DeleteAgentRuntimeEndpoint`,它能按名称限定,已放在 `AgentRuntime` 语句里。这几项 CloudTrail 和错误信息里都看不到(CloudFormation 只报 `Access denied for operation 'AWS::BedrockAgentCore::Runtime'`),是用同一策略直接调用 API 才查出来的。

## 实测结果(2026-11-10,us-west-2)

- 新建角色 `PiiRedactionDeployer`,只挂 `least-privilege-deploy-policy.json`(qualifier 换成 `piired`),用它完成了全部步骤:
  - `cdk bootstrap --qualifier piired --toolkit-stack-name CDKToolkit-PiiRedaction`,包括创建 `PiiRedactionCdkExecution` 策略;
  - 首次 `cdk deploy` 因缺少上面的 AgentCore 端点权限失败,CloudFormation 用收紧后的执行角色**完整回滚**,删除了已创建的约 100 个资源;
  - 补上权限后重新部署,CDK 先删除 `ROLLBACK_COMPLETE` 的栈再创建,119 个资源全部成功。
- 部署后验证:首个管理员收到邀请(`FORCE_CHANGE_PASSWORD`、在 `admins` 组);无 token 401,普通用户访问 `/rules`、`/users` 403,管理员 200。

### 后续补验(同日,同一套 `piired` 环境)

- **Cognito 动作清单**:之前认为 CloudTrail 查不到 Cognito 事件,这是错的。按部署时间窗口查到了创建路径的 7 个动作,据此把 `cognito-idp:*` 换成明确清单,之后三次创建、一次更新、三次删除全部成功。
- **带变更的更新**:临时改了 API 阶段限流、用户池(临时密码有效期)、用户池客户端(刷新令牌有效期)、两个 S3 桶的生命周期、一个 Lambda 的环境变量,部署成功;改回原值再部署一次,也成功。
- **`cdk destroy`**:用最小权限身份执行,两个桶里各放了一个占位文件,桶被自动清空并删除;表、队列、Lambda、事件源映射、用户池、AgentCore Runtime、Guardrail、IAM 角色、API 都不残留。第一次 destroy 因缺少事件源映射的 Get/Delete 权限失败,补上后成功。
- **日志组残留(已知,无害)**:删除栈时,自定义资源背后的 5 个 Lambda 会被最后调用一次,Lambda 异步投递日志,会在日志组被删之后把它重新建出来,且没有保留期。调整删除顺序没有用(事件时间线显示顺序正确,日志组仍被重建)。这 5 个函数的日志里只有桶名和 CloudFormation 请求信息,没有文档内容或用户数据,所以不再为此增加处理逻辑:destroy 后会留下几个空的、没有保留期的 `/aws/lambda/PiiDocumentRedactionPocSt-*` 日志组,可手动删除。

## 未验证的部分

- **IAM Access Analyzer 策略生成**:上面的验证只覆盖本应用实际用到的动作。生产环境可以用 [策略生成](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-policy-generation.html) 根据 CloudTrail 进一步收紧 `s3:*`、`lambda:*`、`apigateway:*` 这三个服务级通配。
- **回滚路径**:第一次部署失败时 CloudFormation 用收紧后的角色回滚过一次,删除路径已经被 destroy 覆盖;但“更新失败后回滚到旧版本”(UPDATE_ROLLBACK)没有单独测过。
- **账号和区域**:策略里的账号和区域是 `*`。固定只部署到 `us-west-2` 的话,可以把 ARN 里的 `*:*` 换成 `us-west-2:<账号ID>`,进一步收紧。

## 参考

- [AWS CDK bootstrapping](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html)
- [Customize AWS CDK bootstrapping](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping-customizing.html)
- [Create and apply permissions boundaries for the AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/customize-permissions-boundaries.html)
