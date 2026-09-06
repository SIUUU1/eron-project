import { Badge } from "@/components/ui/badge";

/** 개발 단계 값임을 시각적으로 못박는 배지. 운영 성능과 섞이면 안 된다. */
export function ReferenceBadge() {
  return (
    <Badge variant="outline" className="border-dashed text-[11px] font-normal">
      Reference / Development only
    </Badge>
  );
}
