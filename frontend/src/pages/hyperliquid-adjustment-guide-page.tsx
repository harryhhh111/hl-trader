import { ArrowLeft, BookOpenCheck } from 'lucide-react';
import { Link } from 'react-router-dom';

import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

const steps = [
  ['选择 pair 和账户', '进入 NBIS / CRWV 详情页，在“账户配置”中确认当前账户。调仓只作用于这个账户的两腿仓位。'],
  ['检查模式和杠杆', '查看两腿下单前检查：支持全仓的合约须使用 Cross，设置杠杆最高 3x。仅支持逐仓的市场允许例外。可先手动调杠杆并确认，修改后重新预览；读取失败禁止增仓，纯减仓和平仓仍可操作。3x 为逐合约设置上限，不代表账户有效杠杆。'],
  ['填写每腿目标名义', '例如输入 2000，表示 NBIS 和 CRWV 各以 2000 USD 为目标，不是两腿合计 2000 USD。'],
  ['先预览差额', '系统重新读取实际仓位和盘口，展示买卖方向、数量与保护价。弹窗每 5 秒更新只读盘口；价格变化后需重新预览，预览不会读取私钥或发送订单。'],
  ['选择签名方式', '托管模式使用账户配置中已加密保存的 API Wallet，输入目标金额确认后由服务端执行。也可导入一次性本地私钥，或使用兼容的浏览器扩展钱包逐腿签名。'],
  ['核对结果与历史', '查看每条腿的成交结果、成交均价、相对预览中间价的滑点和最终仓位。页面会刷新只读快照，底部“交易历史”保留本系统调仓的目标、两腿订单和执行状态；实际成交记录以交易所为准。'],
];

export function HyperliquidAdjustmentGuidePage() {
  return (
    <div className="space-y-4">
      <PageHeader icon={BookOpenCheck} title="调仓使用说明"
        description="以 NBIS 多 / CRWV 空为例，说明目标仓位、下单确认与异常处理。">
        <Button asChild variant="outline">
          <Link to="/hyperliquid/pairs/nbis-crwv"><ArrowLeft className="mr-2 h-4 w-4" />返回 NBIS / CRWV</Link>
        </Button>
      </PageHeader>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">怎么操作</CardTitle>
          <CardDescription>先预览、再确认；不会在输入目标金额后立即下单。</CardDescription>
        </CardHeader>
        <CardContent>
          <ol className="space-y-3 text-sm">
            {steps.map(([title, description], index) => (
              <li key={title} className="grid grid-cols-[1.75rem_1fr] gap-2">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-primary/10 text-xs font-semibold text-primary">{index + 1}</span>
                <div><p className="font-medium">{title}</p><p className="text-muted-foreground">{description}</p></div>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">目标名义与差额</CardTitle>
          <CardDescription>目标是每腿的绝对名义，系统只交易差额；默认方向为第一腿多、第二腿空。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div className="overflow-x-auto">
            <table className="w-full whitespace-nowrap">
              <thead><tr className="border-b text-left text-xs text-muted-foreground">
                {['当前每腿名义', '设置目标', '系统行为'].map((heading) => <th key={heading} className="py-2 pr-4">{heading}</th>)}
              </tr></thead>
              <tbody>
                {[
                  ['1500 USD', '2000 USD', '增加约 500 USD；NBIS 买入，CRWV 卖出增加空仓'],
                  ['2500 USD', '2000 USD', '减少约 500 USD；使用 reduce-only，方向与增仓相反'],
                  ['1990 USD', '2000 USD', '差额约 10 USD，可生成增仓单，但仍须通过精度和保护价校验'],
                  ['1995 USD', '2000 USD', '差额约 5 USD，低于最低下单名义，提示并跳过该腿；另一腿不受影响'],
                  ['任意仓位', '0 USD', '以 reduce-only 平掉该腿；两腿都按 0 处理'],
                ].map(([current, target, behavior]) => (
                  <tr key={current} className="border-b last:border-0">
                    <td className="py-2 pr-4 font-mono">{current}</td>
                    <td className="py-2 pr-4 font-mono">{target}</td>
                    <td className="py-2 pr-4">{behavior}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-muted-foreground">两腿会一起预检；差额低于最低下单名义的腿会显示提示并跳过，不再阻塞另一腿预览或下单。</p>
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle className="text-base">订单怎样成交</CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm text-muted-foreground">
            <p>每腿每次动作至多发送一笔 IOC 限价单：可立即成交的部分成交，未成交部分由交易所取消，不会长期挂在盘口。</p>
            <p>保护价用于限制可接受的成交价格，不代表保证成交价或保证全部成交。减仓先于增仓，减仓单使用 reduce-only，避免反向开仓。</p>
            <p>数量按市场精度向下取整，所以最终名义可能略低于输入目标。最低下单差额按 10 USD 检查，每腿目标硬上限默认为 15000 USD。</p>
            <p>两腿不是原子交易。拒单或部分成交后停止后续写操作，不自动追单，也不自动反向补偿。</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle className="text-base">结果状态怎么处理</CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm">
            <p><span className="font-medium">调仓预览：</span><span className="text-muted-foreground">只显示计划，尚未下单。</span></p>
            <p><span className="font-medium">调仓已完成：</span><span className="text-muted-foreground">两腿订单均已全部成交，并已核对交易所实际持仓。</span></p>
            <p><span className="font-medium">调仓未完成：</span><span className="text-muted-foreground">发生拒单或部分成交。可以重新预览最新差额，但系统不会自动补单。</span></p>
            <p><span className="font-medium text-rose-600">调仓结果未知：</span><span className="text-muted-foreground">可能发生超时、断线或持仓核对不一致。禁止直接重试，应记录 action ID 和 cloid，先人工核对交易所订单、成交和实际持仓。</span></p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader><CardTitle className="text-base">使用前检查</CardTitle></CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          <ul className="list-disc space-y-1.5 pl-5">
            <li>确认当前账户和 pair；切换账户后必须重新预览。</li>
            <li>确保 API Wallet 已在 Hyperliquid 上获得交易账户授权。本地签名模式只在当前页面内存中保存私钥，不写入 localStorage，不上传服务器；关闭弹窗、刷新或切换账户会清除。</li>
            <li>托管钱包在账户配置中添加：私钥经浏览器加密后上传，服务端再加密存入数据库；保存和执行前都会实时核对真实授权。删除密文不等于撤销 Agent，仍需在 Hyperliquid 撤销。</li>
            <li>托管调仓使用持久化 request_id。超时后页面查询原请求，不会生成新订单；结果未知时禁止直接重试，并按 request_id、action ID、cloid 和实际仓位核对。</li>
            <li>本地签名不弹出钱包确认，点击“本地签名并下单”前必须核对两腿订单。只能输入 API Wallet 私钥，禁止输入原交易账户私钥。</li>
            <li>连接和签名的必须是 API Wallet 地址，不能使用原交易账户。后端会恢复并核对签名地址，错误签名会被拒绝。</li>
            <li>目标两腿有挂单、已有反向持仓，或签名前后仓位发生变化时，系统会拒绝提交，需重新处理并预览。</li>
            <li>系统不会定时或自动调仓。每次真实执行都需要用户明确点击签名并下单。</li>
            <li>IOC、滑点、流动性、保证金和强平风险仍由交易者承担；不要把目标名义当作保证成交或风险上限。</li>
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
