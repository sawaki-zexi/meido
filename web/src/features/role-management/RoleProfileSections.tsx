import { useId } from 'react';
import type { RoleProfile } from '../../api/types';
import { Icon } from '../../ui/Icon';

export type ProfileSection = 'profile' | 'personality' | 'behaviorRules' | 'responseConstraints';
const sections: { key: ProfileSection; label: string; placeholder: string }[] = [
  { key: 'profile', label: '角色设定', placeholder: '描述她的身份、背景，以及你们之间的关系' },
  { key: 'personality', label: '性格', placeholder: '例如：温柔、细心，偶尔会开些小玩笑' },
  { key: 'behaviorRules', label: '执行规则', placeholder: '描述她平时会怎么做、怎么回应你' },
  { key: 'responseConstraints', label: '回复约束', placeholder: '例如：避免替你做决定，回答尽量简洁' },
];

export function RoleProfileSections({ profile, expanded, onExpand, onChange, disabled }: {
  profile: RoleProfile; expanded: ProfileSection | null; onExpand: (key: ProfileSection | null) => void;
  onChange: (key: ProfileSection, value: string) => void; disabled: boolean;
}) {
  const id = useId();
  return <div className="role-profile-sections">{sections.map(field => {
    const open = expanded === field.key;
    return <section className={`role-profile-section${open ? ' is-open' : ''}`} key={field.key}>
      <h2><button type="button" id={`${id}-${field.key}-heading`} aria-expanded={open} aria-controls={`${id}-${field.key}`} disabled={disabled}
        onClick={() => onExpand(open ? null : field.key)}>{field.label}<Icon name="back" size={18} /></button></h2>
      <div id={`${id}-${field.key}`} hidden={!open} role="region" aria-labelledby={`${id}-${field.key}-heading`}>
        {open && <textarea name={field.key} aria-label={field.label} value={profile[field.key]} disabled={disabled}
          placeholder={field.placeholder} rows={7} onChange={event => onChange(field.key, event.target.value)} />}
      </div>
      {!open && <p className={`role-profile-summary${profile[field.key].trim() ? '' : ' is-empty'}`}>{profile[field.key].trim() || '未填写'}</p>}
    </section>;
  })}</div>;
}
