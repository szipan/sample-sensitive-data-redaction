# 部署最小权限说明

本目录有五份 IAM 策略,按用途分成三类:

| 文件 | 托管策略名(建议) | 附加给谁 | 作用 |
| --- | --- | --- | --- |
| `least-privilege-deploy-policy.json` | 自定 | 执行 `cdk deploy` / `cdk diff` 的**部署身份**(IAM 用户或角色),长期附加 | 只能扮演 CDK 的 deploy / file-publishing / lookup 角色、读取本应用的栈,以及在本应用的用户池上做管理员恢复。不能创建资源,也不能修改 IAM |
| `least-privilege-bootstrap-policy.json` | 自定 | 部署身份,**只在 `cdk bootstrap` 时临时附加**,完成后立即解除 | 创建 `CDKToolkit` 栈、CDK 资产桶、bootstrap 角色和版本参数。它能修改 bootstrap 角色,所以不能长期保留;以后重新 bootstrap(例如升级 CDK)时再临时挂上 |
| `least-privilege-cfn-exec-policy.json` | `PiiRedactionCdkExecution` | CDK 的 **CloudFormation 执行角色**(独立 qualifier,可选加固) | 替换 bootstrap 默认的 `AdministratorAccess`,只允许创建、更新、删除本应用的资源(IAM 以外) |
| `least-privilege-cfn-exec-iam-policy.json` | `PiiRedactionCdkExecutionIam` | 同上,和上一份一起挂给执行角色 | 本应用的 IAM 角色操作。执行角色只能创建和修改带 `PiiRedactionAppBoundary` 权限边界的角色 |
| `least-privilege-app-boundary-policy.json` | `PiiRedactionAppBoundary` | 不直接附加:作为栈创建的**所有 IAM 角色的权限边界** | 应用角色(Lambda、AgentCore Runtime、自定义资源)最多能做什么。不含任何 `iam:*` 或 `sts:*` |

执行策略拆成两份,是因为加上边界条件后一份会超过托管策略 6144 字符的上限。前两份是部署身份的权限,后三份管 CloudFormation 能建什么、建出来的角色能做什么。栈里每个角色自己的策略仍按资源收敛,边界只是它们的上限。

## 依据

策略来自本账号实际部署的 CloudTrail 记录(2026-11-03 多次 `cdk deploy`,包括新建 Cognito、授权器、用户管理接口的更新),并参照 `kiro-dashboard-on-agentcore/docs/least-privilege-deploy-policy.json` 的格式。

CloudTrail 显示,用现代 CDK bootstrap 部署时:

- **部署身份本人**只调用了 `sts:AssumeRole`(扮演 `cdk-hnb659fds-deploy-role`、`file-publishing-role`、`lookup-role`)和几个只读查询。
- **资源全部由 CloudFormation 执行角色创建**(`cdk-hnb659fds-cfn-exec-role`):Lambda、S3、DynamoDB、SQS、API Gateway、Cognito、Bedrock Guardrail、AgentCore Runtime、IAM 角色。
- 本应用没有 Docker 镜像资产,所以不需要 `image-publishing-role` 和 ECR 推送权限。

五份策略都通过了 IAM Access Analyzer `validate-policy`(0 个问题),压缩后都在托管策略 6144 字符上限内(约 1.1KB、1.6KB、5.0KB、1.5KB、2.4KB)。另用 IAM 策略模拟器核对了提权路径:部署身份不能修改 CDK 角色或执行策略;执行角色不能创建不带边界的角色、不能挂 `AdministratorAccess`、不能删除边界;边界不允许任何 IAM 操作,也不允许访问本应用以外的桶、表和用户池。

## 日常部署策略(`least-privilege-deploy-policy.json`)

| Sid | 用途 |
| --- | --- |
| `CdkAssumeDeployRoles` | 扮演 CDK 的 deploy / file-publishing / lookup 角色,`cdk deploy` / `cdk diff` 只需要这一条 |
| `AppStackRead` | 读取本应用栈的状态和输出(README 第 5 步取 API 地址和用户池 ID) |
| `ApiEndpointLookup` | `aws apigatewayv2 get-apis`,只能列 API,不能修改 |
| `AdminRecoveryOnUserPool` | README 里"所有管理员都无法登录"时,用 CLI 把某人加回 `admins` 组。只对带 `PiiRedactionApp=true` 标签的用户池生效(栈给本应用的用户池打了这个标签)。不需要可删除 |
| `AccountWideUnscopable` | `sts:GetCallerIdentity`、`cloudformation:ValidateTemplate` / `GetTemplateSummary`:不支持资源级限定 |

部署身份本身**没有**任何 Lambda、S3 数据、DynamoDB、IAM 写权限,也不能 `iam:PassRole`(由 deploy 角色把执行角色交给 CloudFormation)。

## bootstrap 策略(`least-privilege-bootstrap-policy.json`)

| Sid | 用途 |
| --- | --- |
| `BootstrapToolkitStack` | 创建和更新 `CDKToolkit` 栈 |
| `BootstrapAssetBucket` / `BootstrapContainerRepo` | CDK 资产桶 `cdk-hnb659fds-*` 和容器仓库 |
| `BootstrapRoles` | 创建和配置 `cdk-hnb659fds-*` 角色 |
| `BootstrapExecutionPolicy` | 读取和更新三个托管策略 `PiiRedactionCdkExecution`、`PiiRedactionCdkExecutionIam`、`PiiRedactionAppBoundary` |
| `BootstrapVersionParameter` | SSM 参数 `/cdk-bootstrap/*/version` |
| `AccountWideUnscopable` | 同上 |

`BootstrapRoles` 允许给 `cdk-hnb659fds-*` 角色挂策略,`BootstrapExecutionPolicy` 允许改执行策略,两者都能用来提权(例如给 deploy 角色挂 `AdministratorAccess` 再扮演它)。所以这份策略**只在运行 `cdk bootstrap` 时附加,完成后立即解除**;以后重新 bootstrap(例如升级 CDK)时再临时挂上。

## 重要:默认 bootstrap 下的实际权限边界

默认 `cdk bootstrap` 给 CloudFormation 执行角色挂的是 **`AdministratorAccess`**,而且这个角色由同一账号、同一区域的所有 CDK 应用共用(本账号的 `KiroDash-*` 等栈也在用)。所以:

- 部署身份策略本身很小,但**能扮演 deploy 角色的人可以通过 CloudFormation 部署任意资源**。
- 不能直接把共用执行角色换成本应用的收敛策略,否则其他 CDK 应用会部署失败。

真正的最小权限要给本应用**单独做一次 bootstrap**(独立 qualifier),执行角色挂两份执行策略,栈里的角色都带权限边界:

```bash
Q=piired                                     # 自选,最多 10 个小写字母或数字
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# 0. 把所有策略里的 hnb659fds 换成你的 qualifier,bootstrap 策略里的 CDKToolkit 换成下面的工具栈名
sed -i "s/hnb659fds/$Q/g" docs/least-privilege-*.json
sed -i "s#stack/CDKToolkit/#stack/CDKToolkit-PiiRedaction/#" docs/least-privilege-bootstrap-policy.json

# 1. 创建三个托管策略(由账号管理员执行)
aws iam create-policy --policy-name PiiRedactionCdkExecution \
  --policy-document file://docs/least-privilege-cfn-exec-policy.json
aws iam create-policy --policy-name PiiRedactionCdkExecutionIam \
  --policy-document file://docs/least-privilege-cfn-exec-iam-policy.json
aws iam create-policy --policy-name PiiRedactionAppBoundary \
  --policy-document file://docs/least-privilege-app-boundary-policy.json

# 2. 给部署身份临时挂 bootstrap 策略(另外长期挂日常部署策略)
aws iam create-policy --policy-name pii-redaction-bootstrap \
  --policy-document file://docs/least-privilege-bootstrap-policy.json
aws iam attach-role-policy --role-name <YOUR_DEPLOY_ROLE> \
  --policy-arn arn:aws:iam::$ACCOUNT:policy/pii-redaction-bootstrap

# 3. 用部署身份做 bootstrap,两份执行策略用逗号分隔
cdk bootstrap aws://$ACCOUNT/us-west-2 \
  --qualifier $Q --toolkit-stack-name CDKToolkit-PiiRedaction \
  --cloudformation-execution-policies "arn:aws:iam::$ACCOUNT:policy/PiiRedactionCdkExecution,arn:aws:iam::$ACCOUNT:policy/PiiRedactionCdkExecutionIam"

# 4. 立即解除 bootstrap 策略
aws iam detach-role-policy --role-name <YOUR_DEPLOY_ROLE> \
  --policy-arn arn:aws:iam::$ACCOUNT:policy/pii-redaction-bootstrap

# 5. 在 backend/cdk.json 的 "context" 里加入两项:使用这个 qualifier,并给栈里所有 IAM 角色加权限边界
#    "@aws-cdk/core:bootstrapQualifier": "piired",
#    "@aws-cdk/core:permissionsBoundary": { "name": "PiiRedactionAppBoundary" }
```

第 5 步的 `@aws-cdk/core:permissionsBoundary` 是 CDK 自带的 context,会给栈里全部 IAM 角色(包括 aws-cdk-lib 内置的自定义资源角色)加 `PermissionsBoundary`。只在这条最小权限路径里加:默认 bootstrap 的账号里没有 `PiiRedactionAppBoundary` 策略,加了会部署失败。之后部署身份只需长期附加改好的 `least-privilege-deploy-policy.json`。

## 执行角色策略(`least-privilege-cfn-exec-policy.json` + `least-privilege-cfn-exec-iam-policy.json`)

资源按本应用的命名收敛:

- **S3**:桶 `piidocumentredactionpocst*`(栈名截断后的前缀);只读 CDK 资产桶,因为创建 Lambda 和 AgentCore Runtime 时要读代码包。
- **Lambda**:函数 `PiiDocumentRedactionPocSt*`、事件源映射。
- **DynamoDB**:表 `RedactionJobs`(含 stream)、`RedactionRules`、`RuleOperations`。
- **SQS**:队列 `RuleOperationsQueue.fifo`。
- **IAM**(在 `least-privilege-cfn-exec-iam-policy.json`):角色 `PiiDocumentRedactionPocSt*`。
  - `AppRoles`:创建、删除、修改角色和内联策略,以及设置边界,都要求 `iam:PermissionsBoundary` 是 `PiiRedactionAppBoundary`。不带这个边界的角色既不能创建,也不能修改。
  - `AppRolesReadAndTag`:读取和打标签,不带条件(这些动作不支持该条件键,也不能改变权限)。
  - `AttachRolePolicy` 只允许挂 `AWSLambdaBasicExecutionRole`,且目标角色必须带同一个边界。
  - 不授予 `DeleteRolePermissionsBoundary`,也不能修改 `PiiRedaction*` 托管策略本身,所以执行角色不能去掉或放宽边界。
  - `PassRole` 只能传给 `lambda.amazonaws.com` 和 `bedrock-agentcore.amazonaws.com`。
  - 只允许为 `bedrock-agentcore` 创建服务关联角色,全新账号首次创建 Runtime 时需要。
- **AgentCore**:Runtime `pii_redaction_agent-*` 及其 workload identity。
- **KMS**:只能经由 S3、Lambda、SQS、DynamoDB 使用(`kms:ViaService` 条件),对应各服务的 AWS 托管密钥。
- **CloudWatch Logs**:`LogGroups` 只允许创建、删除和配置本应用的日志组 `/aws/*/PiiDocumentRedactionPoc*`,即 API 访问日志组 `/aws/apigateway/PiiDocumentRedactionPocApi-access` 和各 Lambda 函数的 `/aws/lambda/PiiDocumentRedactionPocSt*`。这些日志组都由栈直接创建并设 1 天保留期。AgentCore Runtime 的日志组由 AgentCore 在创建 Runtime 时用 Runtime 的执行角色自己建,不经过 CloudFormation 执行角色;栈用只能操作这一个日志组的自定义资源设置保留期,并在删除栈时删除它;`ApiLogDelivery` 是 API Gateway 开启访问日志时要求的日志投递权限(`*LogDelivery`、`PutResourcePolicy` 等),这些动作不支持资源级限定,只能用 `"Resource": "*"`。

以下三组保留了服务级通配的动作,资源已限定。直接用于生产前,应把它们收紧为 CloudFormation 实际调用的动作清单(见“未验证的部分”):

| Sid | 动作 | 资源 | 原因 |
| --- | --- | --- | --- |
| `AppBuckets` | `s3:*` | 本应用两个桶 | CloudFormation 读写桶配置的动作很多(CloudTrail 里约 30 个),删除栈时还要清空和删除桶 |
| `AppFunctions` | `lambda:*` | 本应用函数 | 创建、更新、删除、调用自定义资源,版本间动作会变化 |
| `HttpApi` / `UserPool` | `apigateway:*` / 明确的 17 个 `cognito-idp:*` 动作 | `/apis*`、`userpool/*` | API Gateway 按 HTTP 方法授权;Cognito 用户池 ID 是随机的,无法按名称限定,但动作已逐个列出(用户池、客户端、组的增删改查、标签),不含任何用户数据操作 |

下列动作不支持资源级限定,只能用 `"Resource": "*"`:`bedrock:CreateGuardrail`、`bedrock-agentcore:CreateAgentRuntime`、`cognito-idp:CreateUserPool`、`lambda:GetAccountSettings`、`iam:GetAccountSummary`,以及 Lambda 事件源映射的 `Create/Get/Update/Delete/ListEventSourceMapping`。后一组是实测补上的:映射已经不存在时(删除后 CloudFormation 的确认查询、删除失败后的重试),Lambda 按资源 `*` 授权,请求里也没有函数 ARN,所以无论按 `event-source-mapping:*` 限定资源,还是加 `lambda:FunctionArn` 条件,都会被拒绝,栈会卡在 DELETE_FAILED(2026-11-11 实测两次)。代价是这个执行角色可以修改和删除账号里任何事件源映射;它只由 CloudFormation 使用,在独立 bootstrap 下也只服务本应用。

`AgentRuntimeCreateTimeChecks` 是实测补上的:`CreateAgentRuntime` 会同时创建 DEFAULT 端点并打标签,这时 Runtime ID 还没生成,AgentCore 用字面量 `runtime/*` 检查 `CreateAgentRuntimeEndpoint` 和 `TagResource`,按名称限定的 `runtime/pii_redaction_agent-*` 匹配不上,只能放开到 `runtime/*`。删除 Runtime 时还要 `DeleteAgentRuntimeEndpoint`,它能按名称限定,已放在 `AgentRuntime` 语句里。这几项 CloudTrail 和错误信息里都看不到(CloudFormation 只报 `Access denied for operation 'AWS::BedrockAgentCore::Runtime'`),是用同一策略直接调用 API 才查出来的。

## 权限边界(`least-privilege-app-boundary-policy.json`)

`PiiRedactionAppBoundary` 是栈里所有 IAM 角色的权限上限。角色实际能做的,是它自己的策略和边界的交集;即使模板里给某个角色写了更大的权限,也超不出边界。边界按本应用的命名限定资源:

| Sid | 允许 |
| --- | --- |
| `AppBuckets` / `ReadCdkAssets` | 本应用的两个桶 `piidocumentredactionpocst*`(`s3:*`,含 S3 自动删除函数通过桶策略获得的权限);CDK 资产桶只读 |
| `AppTables` / `ListStreams` | 表 `RedactionJobs`(含 stream)、`RedactionRules`、`RuleOperations`;`dynamodb:ListStreams` 不支持资源级限定 |
| `AppQueue` | 队列 `RuleOperationsQueue.fifo` |
| `AppFunctions` | 调用和读取本应用的函数(自定义资源框架函数调用处理函数) |
| `AppLogGroups` / `DescribeLogGroups` | 本应用的 Lambda 日志组和 AgentCore Runtime 日志组(含 `AWSLambdaBasicExecutionRole` 的日志写入) |
| `AppUserPool` | Cognito 管理员 API,只对带 `PiiRedactionApp=true` 标签的用户池 |
| `BedrockModels` / `Guardrails` | 调用模型和推理配置文件、应用 Guardrail |
| `AgentRuntime` / `AgentCoreMetrics` | 调用本应用的 Runtime、获取 workload access token、写 `bedrock-agentcore` 命名空间的指标 |
| `NoResourceLevelPermissions` | `textract:DetectDocumentText` 和 X-Ray 写入,不支持资源级限定 |

边界不允许任何 `iam:*` 或 `sts:*`,所以应用角色不能创建或修改角色,也不能扮演其他角色。资源策略(例如桶策略)把权限授予角色 ARN 时,同样受边界限制。

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
- **日志组残留(已知,无害)**:删除栈时,自定义资源背后的 6 个 Lambda 会被最后调用一次,Lambda 异步投递日志,会在日志组被删之后把它重新建出来,且没有保留期。调整删除顺序没有用(事件时间线显示顺序正确,日志组仍被重建)。这 6 个函数的日志里只有桶名、日志组名和 CloudFormation 请求信息,没有文档内容或用户数据,所以不再为此增加处理逻辑:destroy 后会留下几个空的、没有保留期的 `/aws/lambda/PiiDocumentRedactionPocSt-*` 日志组,可手动删除。
- **事件源映射加条件的尝试**(2026-11-11):给事件源映射的 Get/Update/Delete 加 `lambda:FunctionArn` 条件后,`cdk destroy` 先因删除后的确认查询 `GetEventSourceMapping` 被拒绝而失败,把 Get 改为无条件后,重试删除又因 `DeleteEventSourceMapping`(映射已不存在)被拒绝。因此恢复为无条件授权;恢复后重试删除、全新部署、`cdk destroy` 和再次全新部署都成功。

## 未验证的部分

- **IAM Access Analyzer 策略生成**:上面的验证只覆盖本应用实际用到的动作。生产环境上线前,应使用 [策略生成](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-policy-generation.html) 根据 CloudTrail 把 `s3:*`、`lambda:*`、`apigateway:*` 这三个服务级通配收紧为明确的动作清单。
- **回滚路径**:第一次部署失败时 CloudFormation 用收紧后的角色回滚过一次,删除路径已经被 destroy 覆盖;但“更新失败后回滚到旧版本”(UPDATE_ROLLBACK)没有单独测过。
- **账号和区域**:策略里的账号和区域是 `*`。固定只部署到 `us-west-2` 的话,可以把 ARN 里的 `*:*` 换成 `us-west-2:<账号ID>`,进一步收紧。

## 参考

- [AWS CDK bootstrapping](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html)
- [Customize AWS CDK bootstrapping](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping-customizing.html)
- [Create and apply permissions boundaries for the AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/customize-permissions-boundaries.html)
