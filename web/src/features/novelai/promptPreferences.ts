export const undesiredContentPresetOptions = [
  { value: 0, label: '不使用' },
  { value: 1, label: '轻度' },
  { value: 2, label: '重度' },
] as const;

export function undesiredContentPresetLabel(value: number | undefined): string {
  if (value === undefined) return '未记录';
  const option = undesiredContentPresetOptions.find((item) => item.value === value);
  return option ? option.label + '（' + option.value + '）' : '未知预设（' + value + '）';
}
