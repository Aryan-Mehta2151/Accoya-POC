export const queryKeys = {
  leads: ['leads'] as const,
  emails: ['emails'] as const,
  email: (emailId: string) => ['email', emailId] as const,
  leadWorkspace: (leadId: string) => ['lead-workspace', leadId] as const,
  documents: ['documents'] as const,
  chatSessions: ['chat-sessions'] as const,
  chatHistory: (sessionId: string) => ['chat-history', sessionId] as const,
};
