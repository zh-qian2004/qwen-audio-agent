import { resolve } from 'node:path'
import { acquireCliInstance } from '../../cli/src/instance-lock.mjs'

export function acquireM1ConnectorInstance(stateDirectory, instanceKey, options = {}) {
  try {
    return acquireCliInstance(resolve(stateDirectory, 'clients', 'm1-robot'), {
      ...options,
      instanceKey,
    })
  } catch (error) {
    if (error?.message === '另一个 qwenaudio CLI 已在运行') {
      throw new Error(`M1 Connector ${instanceKey || 'default'} 已在运行`, { cause: error })
    }
    throw error
  }
}
