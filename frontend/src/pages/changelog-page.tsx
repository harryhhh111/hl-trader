import { History } from 'lucide-react';

import { PageHeader } from '@/components/layout/page-header';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

type ChangeKind = '新功能' | '体验优化' | '安全';

interface Change {
  kind: ChangeKind;
  text: string;
}

interface Release {
  date: string;
  title: string;
  description: string;
  changes: Change[];
}

const releases: Release[] = [
  {
    date: '2026-11-12',
    title: 'Pair 风控与下单前检查',
    description: '补充整体止盈止损、杠杆管理和共享钱包的账户边界。',
    changes: [
      { kind: '新功能', text: '新增 Pair 整体浮盈亏止盈止损监控；触发后由用户重新预览并人工确认平仓，不会自动下单。' },
      { kind: '新功能', text: '新增逐合约杠杆和 Cross / isolated 调整；增仓前检查最高 3x，并在签名与发送前重复核对。' },
      { kind: '体验优化', text: '小于 10 USD 的单腿差额会提示并跳过，不再阻塞另一腿；杠杆修改后自动刷新检查并要求重新预览。' },
      { kind: '体验优化', text: '每腿发送前刷新盘口并更新托管订单保护价；浏览器订单价格变化时要求重新签名，未执行的腿明确显示“未发送”。' },
      { kind: '安全', text: '同一交易账户可跨 pair 共用托管 API Wallet，并通过共享写锁串行化写入；跨交易账户复用仍被禁止。' },
    ],
  },
  {
    date: '2026-11-11',
    title: '调仓过程与结果更清晰',
    description: '补充调仓时的实时价格依据和成交后的滑点信息。',
    changes: [
      { kind: '新功能', text: '新增网站更新日志页面，集中查看已经上线的功能改动。' },
      { kind: '新功能', text: '调仓弹窗每 5 秒更新两腿盘口，展示买一、卖一和中间价；盘口变化后必须重新预览。' },
      { kind: '体验优化', text: '调仓结果和交易历史展示每腿成交均价、滑点百分比与滑点金额，并标明是否有利。' },
    ],
  },
  {
    date: '2026-11-04',
    title: '目标仓位调整与托管 API Wallet',
    description: '支持按每腿目标名义调整 Hyperliquid pair 仓位。',
    changes: [
      { kind: '新功能', text: '根据实际仓位和目标名义预览两腿差额，展示交易方向、数量与 IOC 保护价。' },
      { kind: '新功能', text: '支持浏览器 API Wallet 逐腿签名，也支持添加托管 API Wallet 执行已确认的调仓。' },
      { kind: '新功能', text: '新增调仓使用说明和交易历史，保留每腿订单、成交状态与最终仓位。' },
      { kind: '安全', text: '托管私钥经浏览器和服务端双重加密保存；执行前后核对授权、仓位和签名账户，异常结果禁止直接重试。' },
    ],
  },
];

const badgeVariant: Record<ChangeKind, 'default' | 'secondary' | 'outline'> = {
  新功能: 'default',
  体验优化: 'secondary',
  安全: 'outline',
};

export function ChangelogPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        icon={History}
        title="更新日志"
        description="按日期记录网站已经上线的功能、体验与安全改动，最新更新排在前面。"
      />

      <div className="space-y-4">
        {releases.map((release) => (
          <Card key={release.date} aria-labelledby={`release-${release.date}`}>
            <CardHeader>
              <time dateTime={release.date} className="text-xs font-medium text-muted-foreground">{release.date}</time>
              <CardTitle id={`release-${release.date}`} className="text-base">{release.title}</CardTitle>
              <CardDescription>{release.description}</CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="space-y-3">
                {release.changes.map((change) => (
                  <li key={change.text} className="flex items-start gap-3 text-sm leading-relaxed">
                    <Badge variant={badgeVariant[change.kind]} className="mt-0.5 shrink-0">{change.kind}</Badge>
                    <span>{change.text}</span>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
