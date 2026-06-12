import * as Headless from '@headlessui/react'
import React, { forwardRef } from 'react'
import { Link as RouterLink } from 'react-router-dom'

export const Link = forwardRef(function Link(
  props: { href: string } & Omit<React.ComponentPropsWithoutRef<typeof RouterLink>, 'to'>,
  ref: React.ForwardedRef<HTMLAnchorElement>
) {
  const { href, ...rest } = props
  return (
    <Headless.DataInteractive>
      <RouterLink to={href} {...rest} ref={ref} />
    </Headless.DataInteractive>
  )
})
